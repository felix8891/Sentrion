from pathlib import Path

from app.currency import detect_currency, format_currency, parse_money_amount
from app.db import get_db
from app.services.extraction import extract_document, extract_fields
from app.services.risk import evaluate_invoice
from .conftest import login_as

ROOT = Path(__file__).resolve().parents[1]


def test_currency_formatter_uses_record_currency_without_changing_numeric_values():
    assert format_currency(4266, 'USD') == 'USD 4,266.00'
    assert format_currency(4266.0, 'IDR') == 'IDR 4,266'
    assert format_currency(4266.5, 'USD') == 'USD 4,266.50'
    assert format_currency(None, 'USD') == '—'


def test_currency_detector_prioritizes_explicit_currency_and_flags_ambiguous_fallback():
    assert detect_currency('Invoice\nCurrency: USD\nTotal: 4,266.00') == ('USD', 'HIGH')
    assert detect_currency('Invoice\nCurrency: EUR\nTotal: 4.266,50') == ('EUR', 'HIGH')
    assert detect_currency('Invoice\nTotal: Rp 4.266') == ('IDR', 'HIGH')
    assert detect_currency('Invoice\nTotal: $4,266.00') == ('USD', 'POLICY')
    assert detect_currency('Supplier address: Chiang Mai, Thailand\nTax Reg.: TH-123456\nTotal: I4,266.00') == ('THB', 'HIGH')
    assert detect_currency('Invoice\nTotal: 4266') == ('USD', 'LOW')


def test_currency_aware_amount_parser_preserves_usd_decimals_and_grouping():
    assert parse_money_amount('USD 4,266.50', 'USD') == 4266.5
    assert parse_money_amount('4,266', 'USD') == 4266.0
    assert parse_money_amount('1.234,50', 'EUR') == 1234.5
    assert parse_money_amount('Rp 1.234.567', 'IDR') == 1234567.0
    assert parse_money_amount('1000.00', 'IDR') == 1000.0


def test_extraction_preserves_usd_cents_in_totals_and_line_items():
    text = '''Invoice Number: INV-DECIMAL-01
Currency: USD
Vendor: Decimal Demo Supplies LLC
Invoice Date: 2026-10-16
Purchase Order Number: PO-DECIMAL-01
Subtotal: USD 1,234.50
Discount: USD 0.00
Tax: USD 98.76
Grand Total: USD 1,333.26
ITEM: Sample Ingredient | QTY: 10 | UNIT: cases | UNIT PRICE: USD 123.45 | TAX: 8.0% | TOTAL: USD 1,333.26
'''
    fields, items = extract_fields(text, '2026-10-17T12:00:00', 'decimal.pdf')
    assert fields['currency'] == 'USD'
    assert fields['subtotal'] == 1234.5
    assert fields['tax'] == 98.76
    assert fields['total'] == 1333.26
    assert items[0]['unit_price'] == 123.45
    assert items[0]['line_total'] == 1333.26


def test_example_invoice_extracts_usd_and_currency_confidence():
    path = ROOT / 'examples' / 'invoices' / '01_invoice_green.pdf'
    text, fields, items, mime = extract_document(path, path.name, '2026-10-17T12:00:00')
    assert fields['currency'] == 'USD'
    assert fields['currency_confidence'] == 'HIGH'
    assert fields['tax'] == 194.0
    assert fields['total'] == 2619.0
    assert len(items) == 2


def test_invoice_po_currency_mismatch_is_red_and_skips_monetary_comparisons(client):
    invoice_id = 'inv-demo-0002'
    with get_db() as conn:
        original = conn.execute('SELECT currency FROM invoices WHERE id=?', (invoice_id,)).fetchone()
        assert original
        conn.execute('UPDATE invoices SET currency=? WHERE id=?', ('EUR', invoice_id))
        result = evaluate_invoice(conn, invoice_id)
        codes = {f['finding_code'] for f in result['findings']}
        assert result['risk_level'] == 'RED'
        assert 'CURRENCY_MISMATCH' in codes
        assert 'PO_MISMATCH' not in codes
        assert 'PO_AMOUNT_VARIANCE' not in codes
        assert 'ITEM_PRICE_MISMATCH' not in codes
        conn.execute('UPDATE invoices SET currency=? WHERE id=?', (original['currency'], invoice_id))
        evaluate_invoice(conn, invoice_id)


def test_ambiguous_invoice_currency_is_yellow_until_personnel_confirms(client):
    invoice_id = 'inv-demo-0002'
    with get_db() as conn:
        old = conn.execute('SELECT currency, raw_text, extracted_json FROM invoices WHERE id=?', (invoice_id,)).fetchone()
        assert old
        conn.execute('UPDATE invoices SET raw_text=?, extracted_json=? WHERE id=?', ('Invoice Number: INV-TEST\nTotal: 100', '{"currency":"USD","currency_confidence":"LOW"}', invoice_id))
        result = evaluate_invoice(conn, invoice_id)
        codes = {f['finding_code'] for f in result['findings']}
        assert 'CURRENCY_UNVERIFIED' in codes
        assert result['risk_level'] in {'YELLOW', 'RED'}
        conn.execute('UPDATE invoices SET currency=?, raw_text=?, extracted_json=? WHERE id=?', (old['currency'], old['raw_text'], old['extracted_json'], invoice_id))
        evaluate_invoice(conn, invoice_id)


def test_money_is_rendered_with_record_currency_on_app_pages(client):
    login_as(client)
    dashboard = client.get('/dashboard')
    assert dashboard.status_code == 200
    assert 'USD 4,266.00' in dashboard.text
    invoice_list = client.get('/invoices')
    assert invoice_list.status_code == 200 and 'USD 4,266.00' in invoice_list.text
    detail = client.get('/invoices/inv-demo-0001')
    assert detail.status_code == 200
    assert 'USD 2,619.00' in detail.text
    assert 'IDR 2,619' not in detail.text
    assert 'USD 2,619.00' in detail.text  # totals and line-item values use the shared formatter
    po_page = client.get('/purchase-orders')
    assert po_page.status_code == 200 and 'USD ' in po_page.text
    client.cookies.clear()
