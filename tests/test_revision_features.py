from __future__ import annotations

import json
from pathlib import Path

import fitz

from app.currency import detect_currency
from app.db import get_db
from app.services.extraction import extract_document
from .conftest import csrf_from, login_as

ROOT = Path(__file__).resolve().parents[1]


def make_po_pdf(po_number: str, *, include_total: bool = True) -> bytes:
    lines = [
        'PURCHASE ORDER', po_number,
        'BUYER / PURCHASING ENTITY',
        'Longford Test Buyer Ltd.',
        '100 Main Street, Test City',
        'Tax Reg.: BUYER-123456',
        'SUPPLIER / VENDOR',
        'Pinecrest Test Supplier LLC',
        '200 Market Road, Test City',
        'Tax Reg.: SUP-654321',
        'PO DATE', '2026-10-20',
        'CURRENCY', 'USD',
        'Description', 'SKU', 'Qty', 'Unit', 'Unit Price', 'Line Disc.', 'Amount',
        'Test Ingredient, 5 kg bag', 'ING-005', '2', 'bag', '$50.00', '-', '$100.00',
        'Subtotal', '$100.00', 'Discount', '$0.00', 'Sales Tax (8.00%)', '$8.00',
    ]
    if include_total:
        lines += ['PO TOTAL', '$108.00']
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    page.insert_textbox(fitz.Rect(40, 35, 555, 810), '\n'.join(lines), fontsize=10, lineheight=1.4)
    data = doc.tobytes()
    doc.close()
    return data


def test_dataset_v4_extractor_handles_party_roles_and_financial_summary():
    # Small deterministic regression fixture; full dataset accuracy is evaluated by
    # the separate benchmark artifact and is deliberately not required by pytest.
    path = ROOT / 'examples' / 'purchase_orders' / '01_purchase_order_upload_example.pdf'
    text, fields, items, mime = extract_document(path, path.name, '2026-10-20T00:00:00Z', document_type='purchase_order')
    assert mime == 'application/pdf'
    assert fields['purchase_order_number']
    assert fields['vendor_company_name']
    assert fields['subtotal'] is not None
    assert fields['total'] is not None
    assert items


def test_currency_policy_uses_usd_for_bare_dollar_but_context_for_encoded_baht():
    assert detect_currency('Invoice\nTotal: $1,234.50') == ('USD', 'POLICY')
    # In this dataset the baht glyph is sometimes broken in the embedded PDF text layer.
    assert detect_currency('Chiang Mai, Thailand\nTax Reg.: TH-123456\nTotal: I1,234.50') == ('THB', 'HIGH')
    # A real dollar sign follows the explicit USD-default policy even for a Thai vendor.
    assert detect_currency('Chiang Mai, Thailand\nTax Reg.: TH-123456\nTotal: $1,234.50') == ('USD', 'POLICY')


def test_po_upload_saves_grand_total_line_items_and_renders_detail(client):
    login_as(client)
    csrf = csrf_from(client, '/purchase-orders')
    data = make_po_pdf('PO-REVISION-TEST-1001')
    response = client.post(
        '/purchase-orders/upload',
        data={'csrf_token': csrf},
        files={'file': ('po-revision-test.pdf', data, 'application/pdf')},
        follow_redirects=False,
    )
    assert response.status_code == 303
    with get_db() as conn:
        po = conn.execute('SELECT * FROM purchase_orders WHERE po_number=?', ('PO-REVISION-TEST-1001',)).fetchone()
        assert po is not None
        assert po['total'] == 108.0
        assert po['total_status'] == 'EXTRACTED'
        assert po['subtotal'] == 100.0
        assert po['vendor_company_name'] == 'Pinecrest Test Supplier LLC'
        assert po['vendor_tax_id'] == 'SUP-654321'
        assert po['buyer_company_name'] == 'Longford Test Buyer Ltd.'
        assert po['buyer_tax_id'] == 'BUYER-123456'
        items = conn.execute('SELECT * FROM purchase_order_items WHERE po_id=?', (po['id'],)).fetchall()
        assert len(items) == 1
        assert items[0]['sku'] == 'ING-005'
        assert items[0]['line_total'] == 100.0
        po_id = po['id']
    detail = client.get(f'/purchase-orders/{po_id}')
    assert detail.status_code == 200
    assert 'Pinecrest Test Supplier LLC' in detail.text
    assert 'Longford Test Buyer Ltd.' in detail.text
    assert 'ING-005' in detail.text
    assert 'USD 108.00' in detail.text
    assert 'Grand total' in detail.text
    source_doc = client.get(f'/po-documents/{po_id}')
    assert source_doc.status_code == 200
    assert source_doc.headers.get('cache-control') == 'private, no-store'

    # The invoice/PO textual supplier may match even when neither is linked to a
    # trusted vendor master record. The option must explain that distinction rather
    # than say the vendor itself was not matched.
    with get_db() as conn:
        seed = conn.execute('SELECT vendor_company_name,purchase_order_number,vendor_id FROM invoices WHERE id=?', ('inv-demo-0002',)).fetchone()
        conn.execute("UPDATE invoices SET vendor_company_name=?,purchase_order_number=?,vendor_id=NULL WHERE id=?",
                     ('Pinecrest Test Supplier LLC','PO-REVISION-TEST-1001','inv-demo-0002'))
    invoice_detail = client.get('/invoices/inv-demo-0002')
    assert invoice_detail.status_code == 200
    assert 'Invoice/PO supplier names match; vendor verification pending' in invoice_detail.text
    assert 'Pinecrest Test Supplier LLC' in invoice_detail.text
    with get_db() as conn:
        conn.execute('UPDATE invoices SET vendor_company_name=?,purchase_order_number=?,vendor_id=? WHERE id=?',
                     (seed['vendor_company_name'],seed['purchase_order_number'],seed['vendor_id'],'inv-demo-0002'))
    client.cookies.clear()


def test_missing_po_grand_total_is_not_replaced_by_subtotal(client):
    login_as(client)
    csrf = csrf_from(client, '/purchase-orders')
    data = make_po_pdf('PO-REVISION-TEST-1002', include_total=False)
    response = client.post(
        '/purchase-orders/upload',
        data={'csrf_token': csrf},
        files={'file': ('po-total-missing.pdf', data, 'application/pdf')},
        follow_redirects=False,
    )
    assert response.status_code == 303
    with get_db() as conn:
        po = conn.execute('SELECT * FROM purchase_orders WHERE po_number=?', ('PO-REVISION-TEST-1002',)).fetchone()
        assert po is not None
        assert po['subtotal'] == 100.0
        assert po['total_status'] == 'MISSING'
        # Legacy storage is non-nullable; the UI must use total_status and never show this 0 as verified.
        po_id = po['id']
    detail = client.get(f'/purchase-orders/{po_id}')
    assert detail.status_code == 200
    assert 'Unavailable — review required' in detail.text
    assert 'Grand total not extracted' in detail.text
    assert '<small>Grand total</small><strong class="money">Unavailable — review required</strong>' in detail.text
    client.cookies.clear()


def test_uploaded_invoice_uses_company_name_only_as_inferred_account_holder(client):
    login_as(client)
    csrf = csrf_from(client, '/invoices')
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    invoice_text = '\n'.join([
        'SUPPLIER INVOICE', 'Invoice Number: INV-ACCOUNT-HOLDER-FALLBACK-01', 'Vendor: Inferred Holder Foods LLC',
        'Vendor Address: 1 Test Road, Test City', 'Tax ID: TEST-TAX-1001',
        'Invoice Date: 2026-10-20', 'Due Date: 2026-11-19', 'Currency: USD',
        'Purchase Order Number: PO-ACCOUNT-HOLDER-01', 'Bank: Test Commerce Bank',
        'Account Number: 123456789012', 'Subtotal: $100.00', 'Tax: $8.00', 'Total Invoice: $108.00',
        'ITEM: Test Ingredient | QTY: 2 | UNIT: bags | UNIT PRICE: 50 | TAX: 8% | TOTAL: 108',
    ])
    page.insert_textbox(fitz.Rect(40, 35, 555, 810), invoice_text, fontsize=11, lineheight=1.4)
    pdf_data = doc.tobytes(); doc.close()
    response = client.post(
        '/invoices/upload',
        data={'csrf_token': csrf},
        files=[('files', ('invoice-no-account-holder.pdf', pdf_data, 'application/pdf'))],
        follow_redirects=False,
    )
    assert response.status_code == 303
    with get_db() as conn:
        row = conn.execute("SELECT * FROM invoices WHERE invoice_number='INV-ACCOUNT-HOLDER-FALLBACK-01' ORDER BY created_at DESC LIMIT 1").fetchone()
        assert row is not None
        extracted = json.loads(row['extracted_json'] or '{}')
        assert extracted['bank_account_holder_name'] == row['vendor_company_name']
        assert extracted['bank_account_holder_name_source'] == 'INFERRED_FROM_SUPPLIER_NAME'
    detail = client.get(f"/invoices/{row['id']}")
    assert detail.status_code == 200
    assert 'Inferred from supplier name' in detail.text
    client.cookies.clear()


def test_po_total_filled_by_optional_llm_requires_review(monkeypatch, client):
    from .test_llm_traceability import _configure_gemini, _completion, FakeResponse
    from app.services import llm_extraction
    import io, json as json_module
    _configure_gemini(monkeypatch)
    monkeypatch.setattr(llm_extraction.urllib.request, 'urlopen', lambda request, timeout: FakeResponse(
        _completion(json_module.dumps({'purchase_order_number':'PO-LLM-CANDIDATE-1003','total':108.00}))
    ))
    login_as(client)
    csrf = csrf_from(client, '/purchase-orders')
    data = make_po_pdf('PO-LLM-CANDIDATE-1003', include_total=False)
    response = client.post('/purchase-orders/upload', data={'csrf_token':csrf},
        files={'file':('po-llm-total-candidate.pdf',data,'application/pdf')},follow_redirects=False)
    assert response.status_code == 303
    with get_db() as conn:
        po=conn.execute('SELECT * FROM purchase_orders WHERE po_number=?',('PO-LLM-CANDIDATE-1003',)).fetchone()
        assert po is not None
        assert po['total'] == 108.0
        assert po['total_status'] == 'REVIEW_REQUIRED'
        assert po['total_source'] == 'EXTERNAL_LLM_CANDIDATE'
        po_id=po['id']
    detail=client.get(f'/purchase-orders/{po_id}')
    assert detail.status_code == 200
    assert 'Grand total requires review' in detail.text
    client.cookies.clear()
