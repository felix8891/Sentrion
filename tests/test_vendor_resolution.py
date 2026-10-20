from __future__ import annotations

import uuid
from datetime import date

import pytest

from app.db import get_db, utcnow
from app.services.risk import evaluate_invoice
from app.services.vendor_resolution import (
    classify_po_vendor_identity,
    link_po_to_vendor,
    relink_unlinked_purchase_orders,
)
from .conftest import csrf_from, login_as


def _insert_vendor(conn, *, name=None, tax_id=None, status='VERIFIED'):
    vendor_id = f'vendor-vr-{uuid.uuid4().hex[:12]}'
    now = utcnow()
    name = name or f'Vendor Resolution Test {vendor_id[-4:]}'
    conn.execute(
        '''INSERT INTO vendors
           (id,vendor_code,company_name,address,tax_id,verification_status,
            current_bank_name,current_bank_account_number,current_bank_account_holder,created_at,updated_at)
           VALUES (?,?,?,?,?,?,NULL,NULL,NULL,?,?)''',
        (vendor_id, f'VR-{vendor_id[-8:]}', name, 'QA Address', tax_id, status, now, now),
    )
    return vendor_id, name, tax_id


def _insert_po(conn, *, po_id=None, po_number=None, vendor_name, tax_id=None,
               vendor_id=None, total=100.0, currency='USD', total_status='EXTRACTED'):
    po_id = po_id or f'po-vr-{uuid.uuid4().hex[:12]}'
    po_number = po_number or f'PO-VR-{uuid.uuid4().hex[:8]}'
    now = utcnow()
    conn.execute(
        '''INSERT INTO purchase_orders
           (id,po_number,vendor_id,issue_date,total,currency,source_filename,source_text,
            status,created_at,vendor_company_name,vendor_tax_id,subtotal,extracted_json,
            currency_confidence,total_status,total_source)
           VALUES (?,?,?,?,?,?,?,?,'ACTIVE',?,?,?,?,?,?,?,?)''',
        (po_id, po_number, vendor_id, date.today().isoformat(), total, currency,
         'qa-synthetic-po.pdf', 'PURCHASE ORDER\nCurrency: USD', now, vendor_name, tax_id,
         total, '{}', 'HIGH', total_status, 'QA_FIXTURE'),
    )
    if vendor_id:
        conn.execute(
            """UPDATE purchase_orders SET vendor_match_status='MATCHED', vendor_match_method='QA_FIXTURE',
               vendor_match_reason='Synthetic test fixture', vendor_match_at=?, vendor_linked_by='usr-admin' WHERE id=?""",
            (now, po_id),
        )
    return po_id, po_number


def _insert_invoice(conn, *, invoice_id=None, vendor_id, vendor_name, tax_id,
                    po_number, subtotal=100.0, total=100.0, currency='USD', line_items=None):
    invoice_id = invoice_id or f'inv-vr-{uuid.uuid4().hex[:12]}'
    now = utcnow()
    conn.execute(
        '''INSERT INTO invoices
           (id,invoice_receive_date,invoice_issue_date,invoice_number,vendor_id,vendor_company_name,
            vendor_tax_id,purchase_order_number,due_date,currency,subtotal,total,extraction_status,
            processing_status,verification_status,approval_status,payment_status,risk_level,
            risk_reasons_json,source_filename,document_hash,raw_text,extracted_json,created_by,created_at,updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'COMPLETED','COMPLETED','PENDING','NOT_REQUESTED',
                   'NOT_EXECUTED','YELLOW','[]',?,?,?,?,?,?,?)''',
        (invoice_id, now, date.today().isoformat(), f'INV-{invoice_id[-8:]}', vendor_id, vendor_name,
         tax_id, po_number, date.today().isoformat(), currency, subtotal, total,
         'qa-synthetic-invoice.pdf', uuid.uuid4().hex * 2, 'Currency: USD',
         '{"currency_confidence":"HIGH"}', 'usr-finance', now, now),
    )
    for idx, item in enumerate(line_items or [], 1):
        conn.execute(
            '''INSERT INTO invoice_items
               (id,invoice_id,item_name,description,quantity,unit,unit_price,tax_rate,item_tax,line_total,line_number)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)''',
            (f'{invoice_id}-item-{idx}', invoice_id, item['name'], None, item['qty'], 'ea',
             item['price'], None, None, item['line_total'], idx),
        )
    return invoice_id


@pytest.fixture

def cleanup_vendor_resolution_records(client):
    """Initialize app schema and remove only records created by the test after use."""
    created = {'vendors': [], 'pos': [], 'invoices': []}
    yield created
    with get_db() as conn:
        for invoice_id in created['invoices']:
            conn.execute('DELETE FROM risk_findings WHERE invoice_id=?', (invoice_id,))
            conn.execute('DELETE FROM invoice_items WHERE invoice_id=?', (invoice_id,))
            conn.execute('DELETE FROM invoice_documents WHERE invoice_id=?', (invoice_id,))
            conn.execute('DELETE FROM audit_events WHERE invoice_id=?', (invoice_id,))
            conn.execute('DELETE FROM invoices WHERE id=?', (invoice_id,))
        for po_id in created['pos']:
            conn.execute('DELETE FROM purchase_order_items WHERE po_id=?', (po_id,))
            conn.execute('DELETE FROM audit_events WHERE instr(details_json, ?) > 0', (po_id,))
            conn.execute('DELETE FROM audit_events WHERE instr(details_json, ?) > 0', (po_id,))
            conn.execute('DELETE FROM purchase_orders WHERE id=?', (po_id,))
        for vendor_id in created['vendors']:
            conn.execute('DELETE FROM vendor_bank_accounts WHERE vendor_id=?', (vendor_id,))
            conn.execute('DELETE FROM vendors WHERE id=?', (vendor_id,))


def test_exact_normalized_tax_id_matches_only_verified_vendor(cleanup_vendor_resolution_records):
    created = cleanup_vendor_resolution_records
    with get_db() as conn:
        vid, name, tax = _insert_vendor(conn, name='Greenfield Pantry Provisions Inc.', tax_id='643-646901')
        created['vendors'].append(vid)
        result = classify_po_vendor_identity(conn, supplier_name=name, supplier_tax_id='643 646901')
    assert result['status'] == 'MATCHED'
    assert result['vendor']['id'] == vid
    assert result['method'] == 'EXACT_TAX_ID'


def test_name_only_match_remains_candidate_until_authorized_confirmation(cleanup_vendor_resolution_records):
    created = cleanup_vendor_resolution_records
    with get_db() as conn:
        vid, name, _ = _insert_vendor(conn, name='Name Only Resolution Supplier LLC', tax_id=None)
        created['vendors'].append(vid)
        result = classify_po_vendor_identity(conn, supplier_name='Name Only Resolution Supplier LLC', supplier_tax_id=None)
    assert result['status'] == 'CANDIDATE'
    assert result['vendor']['id'] == vid
    assert result['method'] == 'EXACT_NAME_CANDIDATE'


def test_conflicting_reliable_tax_ids_are_not_automatically_matched(cleanup_vendor_resolution_records):
    created = cleanup_vendor_resolution_records
    with get_db() as conn:
        vid, name, _ = _insert_vendor(conn, name='Conflicting Tax Supplier LLC', tax_id='TAX-111')
        created['vendors'].append(vid)
        result = classify_po_vendor_identity(conn, supplier_name=name, supplier_tax_id='TAX-222')
    assert result['status'] == 'IDENTITY_CONFLICT'
    assert result['vendor'] is None


def test_ambiguous_name_candidates_do_not_select_arbitrary_vendor(cleanup_vendor_resolution_records):
    created = cleanup_vendor_resolution_records
    with get_db() as conn:
        for _ in range(2):
            vid, _, _ = _insert_vendor(conn, name='Duplicate Exact Name Supplier Ltd.', tax_id=None)
            created['vendors'].append(vid)
        result = classify_po_vendor_identity(conn, supplier_name='Duplicate Exact Name Supplier Ltd.', supplier_tax_id=None)
    assert result['status'] == 'CANDIDATE'
    assert result['vendor'] is None
    assert result['method'] == 'AMBIGUOUS_NAME_CANDIDATES'


def test_approved_vendor_relinks_existing_po_and_resolves_only_vendor_link_finding(cleanup_vendor_resolution_records):
    created = cleanup_vendor_resolution_records
    with get_db() as conn:
        vid, name, tax = _insert_vendor(conn, name='Relinkable Pantry Supplier Inc.', tax_id='VR-TAX-123')
        created['vendors'].append(vid)
        po_id, po_number = _insert_po(conn, vendor_name=name, tax_id='VR-TAX-123')
        created['pos'].append(po_id)
        invoice_id = _insert_invoice(conn, vendor_id=None, vendor_name=name, tax_id=tax, po_number=po_number)
        created['invoices'].append(invoice_id)
        evaluate_invoice(conn, invoice_id)
        active_before = conn.execute("SELECT finding_code FROM risk_findings WHERE invoice_id=? AND status='ACTIVE'", (invoice_id,)).fetchall()
        assert any(row['finding_code'] == 'PO_VENDOR_LINK_REVIEW' for row in active_before)

        summary = relink_unlinked_purchase_orders(conn, vid, actor={'id':'usr-admin','role':'admin'})
        assert po_id in summary['linked_po_ids']
        po = conn.execute('SELECT vendor_id,vendor_match_status,vendor_match_method FROM purchase_orders WHERE id=?', (po_id,)).fetchone()
        assert po['vendor_id'] == vid
        assert po['vendor_match_status'] == 'MATCHED'
        assert po['vendor_match_method'] == 'EXACT_TAX_ID'
        active_after = conn.execute("SELECT finding_code FROM risk_findings WHERE invoice_id=? AND status='ACTIVE'", (invoice_id,)).fetchall()
        history = conn.execute("SELECT finding_code,status FROM risk_findings WHERE invoice_id=?", (invoice_id,)).fetchall()
        assert not any(row['finding_code'] == 'PO_VENDOR_LINK_REVIEW' for row in active_after)
        assert any(row['finding_code'] == 'PO_VENDOR_LINK_REVIEW' and row['status'] == 'SUPERSEDED' for row in history)
        # Missing bank details or any other independent warning must not be silently cleared.
        assert any(row['finding_code'] == 'MISSING_BANK_ACCOUNT' for row in active_after)
        linked_events = conn.execute("SELECT COUNT(*) n FROM audit_events WHERE event_type='PO_VENDOR_LINKED' AND instr(details_json, ?) > 0", (po_number,)).fetchone()['n']
        assert linked_events == 1


def test_relinking_is_idempotent(cleanup_vendor_resolution_records):
    created = cleanup_vendor_resolution_records
    with get_db() as conn:
        vid, name, tax = _insert_vendor(conn, name='Idempotent Link Supplier LLC', tax_id='VR-TAX-IDEMP')
        created['vendors'].append(vid)
        po_id, po_number = _insert_po(conn, vendor_name=name, tax_id=tax)
        created['pos'].append(po_id)
        first = relink_unlinked_purchase_orders(conn, vid, actor={'id':'usr-admin','role':'admin'})
        second = relink_unlinked_purchase_orders(conn, vid, actor={'id':'usr-admin','role':'admin'})
        events = conn.execute("SELECT COUNT(*) n FROM audit_events WHERE event_type='PO_VENDOR_LINKED' AND instr(details_json, ?) > 0", (po_number,)).fetchone()['n']
        assert po_id in first['linked_po_ids']
        assert second['linked_po_ids'] == []
        assert events == 1


def test_name_only_link_requires_explicit_reviewer_confirmation(cleanup_vendor_resolution_records):
    created = cleanup_vendor_resolution_records
    with get_db() as conn:
        vid, name, _ = _insert_vendor(conn, name='Manual Name Confirmation Vendor Ltd.', tax_id=None)
        created['vendors'].append(vid)
        po_id, _ = _insert_po(conn, vendor_name=name, tax_id=None)
        created['pos'].append(po_id)
        with pytest.raises(ValueError, match='explicit identity confirmation'):
            link_po_to_vendor(conn, po_id=po_id, vendor_id=vid, actor={'id':'usr-supervisor','role':'supervisor'},
                              reason='Reviewed supplier name and record')
        result = link_po_to_vendor(conn, po_id=po_id, vendor_id=vid, actor={'id':'usr-supervisor','role':'supervisor'},
                                   reason='Verified company identity against the supplied registration evidence.',
                                   confirm_name_only=True)
        row = conn.execute('SELECT vendor_id,vendor_match_method FROM purchase_orders WHERE id=?', (po_id,)).fetchone()
    assert result['method'] == 'AUTHORIZED_NAME_CONFIRMATION'
    assert row['vendor_id'] == vid
    assert row['vendor_match_method'] == 'AUTHORIZED_NAME_CONFIRMATION'


def test_tax_conflict_override_keeps_a_red_risk_finding(cleanup_vendor_resolution_records):
    created = cleanup_vendor_resolution_records
    with get_db() as conn:
        vid, name, tax = _insert_vendor(conn, name='Conflict Override Test Supplier LLC', tax_id='MASTER-TAX-0001')
        created['vendors'].append(vid)
        po_id, po_number = _insert_po(conn, vendor_name=name, tax_id='DOCUMENT-TAX-9999')
        created['pos'].append(po_id)
        invoice_id = _insert_invoice(conn, vendor_id=vid, vendor_name=name, tax_id=tax, po_number=po_number)
        created['invoices'].append(invoice_id)
        with pytest.raises(ValueError, match='conflicting tax IDs'):
            link_po_to_vendor(conn, po_id=po_id, vendor_id=vid, actor={'id':'usr-admin','role':'admin'},
                              reason='Name matches', override_tax_conflict=False)
        link_po_to_vendor(conn, po_id=po_id, vendor_id=vid, actor={'id':'usr-admin','role':'admin'},
                          reason='Reviewed original tax documents and explicitly authorized this exception.',
                          override_tax_conflict=True)
        active = conn.execute("SELECT finding_code,severity FROM risk_findings WHERE invoice_id=? AND status='ACTIVE'", (invoice_id,)).fetchall()
    assert any(row['finding_code'] == 'PO_VENDOR_TAX_ID_MISMATCH' and row['severity'] == 'RED' for row in active)


def test_vendor_link_resolution_endpoint_denies_finance_user(cleanup_vendor_resolution_records, client):
    created = cleanup_vendor_resolution_records
    with get_db() as conn:
        vid, name, tax = _insert_vendor(conn, name='Endpoint Authorization Test LLC', tax_id='AUTH-TAX-001')
        created['vendors'].append(vid)
        po_id, _ = _insert_po(conn, vendor_name=name, tax_id=tax)
        created['pos'].append(po_id)
    login_as(client, email='finance@sentrion.local', password='FinanceDemo!2026')
    csrf = csrf_from(client, f'/purchase-orders/{po_id}')
    response = client.post(f'/purchase-orders/{po_id}/link-vendor', data={
        'csrf_token': csrf, 'vendor_id': vid, 'reason': 'Test unauthorized role access',
    }, follow_redirects=False)
    assert response.status_code == 403
    with get_db() as conn:
        po = conn.execute('SELECT vendor_id FROM purchase_orders WHERE id=?', (po_id,)).fetchone()
    assert po['vendor_id'] is None
    client.cookies.clear()


def test_identity_conflict_status_generates_red_risk_finding(cleanup_vendor_resolution_records):
    created = cleanup_vendor_resolution_records
    with get_db() as conn:
        vid, name, tax = _insert_vendor(conn, name='Identity Conflict Red Finding Vendor LLC', tax_id='MASTER-TAX-001')
        created['vendors'].append(vid)
        po_id, po_number = _insert_po(conn, vendor_name=name, tax_id='DOCUMENT-TAX-002')
        created['pos'].append(po_id)
        classification = classify_po_vendor_identity(conn, supplier_name=name, supplier_tax_id='DOCUMENT-TAX-002')
        from app.services.vendor_resolution import _write_po_match
        _write_po_match(conn, po_id, status=classification['status'], method=classification.get('method'),
                        reason=classification['reason'], vendor_id=None)
        invoice_id = _insert_invoice(conn, vendor_id=vid, vendor_name=name, tax_id=tax, po_number=po_number)
        created['invoices'].append(invoice_id)
        evaluate_invoice(conn, invoice_id)
        active = conn.execute("SELECT finding_code,severity FROM risk_findings WHERE invoice_id=? AND status='ACTIVE'", (invoice_id,)).fetchall()
    assert any(row['finding_code'] == 'PO_VENDOR_IDENTITY_CONFLICT' and row['severity'] == 'RED' for row in active)
