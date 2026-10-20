"""Safely migrate only Sentrion's seeded demo records to English/USD.

Dry-run is the default. Stop the server, make a backup, and pass --apply to write changes.
The script intentionally preserves historical audit events and appends new migration events.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from app.config import DB_PATH
from app.db import get_db, utcnow
from app.demo_dataset import VENDORS, calculate_invoice_amounts, invoice_number_for_demo, invoice_text, po_number_for_demo_invoice
from app.services.audit import audit
from app.services.risk import evaluate_invoice

LEGACY_VENDORS = {
    'vendor-001': {'name': 'CV Pangan Nusantara', 'account': '1000000001', 'tax_id': '12.345.678.9-101.000'},
    'vendor-002': {'name': 'PT Kemasan Prima', 'account': '1000000002', 'tax_id': '23.456.789.0-202.000'},
    'vendor-003': {'name': 'CV Segar Makmur', 'account': '2000000003', 'tax_id': '34.567.890.1-303.000'},
    'vendor-004': {'name': 'PT Minuman Sejahtera', 'account': '1000000004', 'tax_id': '45.678.901.2-404.000'},
    'vendor-005': {'name': 'CV Beku Jaya', 'account': '2000000005', 'tax_id': '56.789.012.3-505.000'},
    'vendor-006': {'name': 'PT Bumbu Andalan', 'account': '1000000006', 'tax_id': '67.890.123.4-606.000'},
}


def _invoice_details(n: int, row: sqlite3.Row) -> dict:
    vendor_index = 0 if n == 49 else (n - 1) % len(VENDORS)
    vendor = VENDORS[vendor_index]
    amounts = calculate_invoice_amounts(n, vendor_index, include_unapproved_item=True)
    bank = vendor['bank']
    account = '889900001234' if n == 46 else vendor['account']
    holder = vendor['name']
    inv_no = invoice_number_for_demo(n)
    po_number = po_number_for_demo_invoice(n)
    issue_date = row['invoice_issue_date'] or datetime.now(timezone.utc).date().isoformat()
    due_date = row['due_date']
    raw_text = invoice_text(
        invoice_number=inv_no, issue_date=issue_date, due_date=due_date,
        vendor=vendor, po_number=po_number, bank_name=bank,
        account_number=account, account_holder=holder,
        amounts=amounts, malicious=(n == 47),
    )
    digest = hashlib.sha256(raw_text.encode('utf-8')).hexdigest()
    extracted = {
        'invoice_number': inv_no,
        'invoice_receive_date': row['invoice_receive_date'],
        'invoice_issue_date': issue_date,
        'vendor_company_name': vendor['name'],
        'vendor_address': vendor['address'],
        'vendor_tax_id': vendor['tax_id'],
        'purchase_order_number': po_number,
        'due_date': due_date,
        'currency': 'USD',
        'currency_confidence': 'HIGH',
        'bank_name': bank,
        'bank_account_number': account,
        'bank_account_holder_name': holder,
        'subtotal': amounts['subtotal'],
        'discount': amounts['discount'],
        'tax': amounts['tax'],
        'total': amounts['total'],
        'items': amounts['items'],
    }
    return {'n': n, 'vendor': vendor, 'amounts': amounts, 'bank': bank, 'account': account,
            'holder': holder, 'inv_no': inv_no, 'po_number': po_number,
            'issue_date': issue_date, 'due_date': due_date,
            'raw_text': raw_text, 'hash': digest, 'extracted': extracted}


def _render_po_text(n: int, vendor: dict, issue_date: str, amounts: dict) -> str:
    po_number = f'PO-2026-{n:04d}'
    lines = [
        'PURCHASE ORDER', f'Purchase Order Number: {po_number}', 'Currency: USD',
        f'Vendor: {vendor["name"]}', f'Vendor Address: {vendor["address"]}',
        f'Tax ID: {vendor["tax_id"]}', f'Issue Date: {issue_date}',
        f'Subtotal: {amounts["subtotal"]:,}', f'Tax: {amounts["tax"]:,}',
        f'Total: {amounts["total"]:,}',
    ]
    for item in amounts['items']:
        lines.append(
            f'ITEM: {item["item_name"]} | QTY: {item["quantity"]} | UNIT: {item["unit"]} '
            f'| UNIT PRICE: {item["unit_price"]} | TAX: {item["tax_rate"]}% | TOTAL: {item["line_total"]}'
        )
    return '\n'.join(lines)


def migrate(apply: bool) -> int:
    if not DB_PATH.exists():
        print(f'Database not found: {DB_PATH}')
        print('Start Sentrion once to create and seed its local database, then stop the server and run this script again.')
        return 2

    with get_db() as conn:
        invoice_rows = conn.execute("SELECT * FROM invoices WHERE id GLOB 'inv-demo-[0-9][0-9][0-9][0-9]' ORDER BY id").fetchall()
        vendor_rows = conn.execute("SELECT * FROM vendors WHERE id IN ('vendor-001','vendor-002','vendor-003','vendor-004','vendor-005','vendor-006') ORDER BY vendor_code").fetchall()
        po_count = conn.execute("SELECT COUNT(*) FROM purchase_orders WHERE id GLOB 'po-demo-[0-9][0-9][0-9][0-9]'").fetchone()[0]
        if len(invoice_rows) != 50 or len(vendor_rows) != 6 or po_count != 50:
            print('Migration stopped: expected the original 50 seeded invoices, 6 demo vendors, and 50 demo POs.')
            print(f'Found: {len(invoice_rows)} invoices, {len(vendor_rows)} vendors, {po_count} POs.')
            print('No changes were made. Review this database manually rather than risking unrelated records.')
            return 3

        already_usd = all(r['currency'] == 'USD' for r in invoice_rows)
        vendors_already_updated = all(
            next((v['name'] for v in VENDORS if v['id'] == row['id']), None) == row['company_name']
            for row in vendor_rows
        )
        pos_already_usd = conn.execute("SELECT COUNT(*) FROM purchase_orders WHERE id GLOB 'po-demo-[0-9][0-9][0-9][0-9]' AND currency <> 'USD'").fetchone()[0] == 0
        if already_usd and vendors_already_updated and pos_already_usd:
            print('Demo dataset already appears to be English/USD. No changes needed.')
            return 0

        pending = conn.execute("""SELECT COUNT(*) FROM approval_requests
            WHERE status='PENDING_SECOND_APPROVER' AND (
                (entity_type='invoice' AND entity_id GLOB 'inv-demo-[0-9][0-9][0-9][0-9]') OR
                (entity_type='vendor' AND entity_id IN ('vendor-001','vendor-002','vendor-003','vendor-004','vendor-005','vendor-006'))
            )""").fetchone()[0]
        if pending:
            print(f'Migration stopped: {pending} pending dual-authorization request(s) reference demo invoices or vendors.')
            print('Resolve or reject those requests first. No changes were made.')
            return 4

    print('Planned update (demo records only):')
    print('- Vendors: 6')
    print('- Invoices: 50')
    print('- Purchase orders: 50')
    print('- Invoice and PO items: English descriptions with USD amounts')
    print('- Historical audit events: preserved; new migration audit events will be appended')
    print('- User-created invoices/vendors and uploaded documents: untouched')
    if not apply:
        print('\nDry run only. To apply, stop the Sentrion server and run:')
        print('  python tools_migrate_demo_data_to_english_usd.py --apply')
        return 0

    backup = DB_PATH.with_name(f'{DB_PATH.stem}.before_english_usd_{datetime.now().strftime("%Y%m%d_%H%M%S")}{DB_PATH.suffix}.bak')
    shutil.copy2(DB_PATH, backup)
    print(f'Created database backup: {backup}')

    with get_db() as conn:
        now = utcnow()
        # Update the six known demo master records only. Preserve account changes that no longer
        # equal the original seeded account, since they may represent user actions in the local demo.
        for vendor in VENDORS:
            old = LEGACY_VENDORS[vendor['id']]
            current = conn.execute('SELECT * FROM vendors WHERE id=?', (vendor['id'],)).fetchone()
            current_account = current['current_bank_account_number']
            if current_account == old['account'] or current_account == vendor['account']:
                bank_name, account_number = vendor['bank'], vendor['account']
            else:
                bank_name, account_number = current['current_bank_name'], current_account
            account_holder = vendor['name'] if current['current_bank_account_holder'] in {old['name'], vendor['name']} else current['current_bank_account_holder']
            conn.execute('''UPDATE vendors SET company_name=?,address=?,tax_id=?,current_bank_name=?,current_bank_account_number=?,current_bank_account_holder=?,updated_at=? WHERE id=?''',
                         (vendor['name'],vendor['address'],vendor['tax_id'],bank_name,account_number,account_holder,now,vendor['id']))
            conn.execute('''UPDATE vendor_bank_accounts SET bank_name=?,account_number=?,account_holder_name=?
                WHERE vendor_id=? AND account_number=?''', (vendor['bank'],vendor['account'],vendor['name'],vendor['id'],old['account']))

        old_hashes = {}
        for row in conn.execute("SELECT * FROM invoices WHERE id GLOB 'inv-demo-[0-9][0-9][0-9][0-9]' ORDER BY id").fetchall():
            n = int(row['id'][-4:])
            details = _invoice_details(n, row)
            old_hashes[row['id']] = {'currency': row['currency'], 'total': row['total'], 'hash': row['document_hash']}
            vendor = details['vendor']; amounts = details['amounts']; extracted = details['extracted']
            conn.execute('''UPDATE invoices SET invoice_number=?,vendor_id=?,vendor_company_name=?,vendor_address=?,vendor_tax_id=?,purchase_order_number=?,currency='USD',
                bank_name=?,bank_account_number=?,bank_account_holder_name=?,subtotal=?,discount=?,tax=?,total=?,document_hash=?,raw_text=?,extracted_json=?,updated_at=? WHERE id=?''',
                (details['inv_no'],vendor['id'],vendor['name'],vendor['address'],vendor['tax_id'],details['po_number'],details['bank'],details['account'],details['holder'],
                 amounts['subtotal'],amounts['discount'],amounts['tax'],amounts['total'],details['hash'],details['raw_text'],json.dumps(extracted,ensure_ascii=False),now,row['id']))

            # Update only the seed-generated item IDs; preserve any unrelated item records.
            for idx, item in enumerate(amounts['items'], 1):
                item_id = f'{row["id"]}-item-{idx}'
                existing = conn.execute('SELECT id FROM invoice_items WHERE id=? AND invoice_id=?',(item_id,row['id'])).fetchone()
                values = (item['item_name'],item['quantity'],item['unit'],item['unit_price'],item['tax_rate'],item['item_tax'],item['line_total'])
                if existing:
                    conn.execute('''UPDATE invoice_items SET item_name=?,quantity=?,unit=?,unit_price=?,tax_rate=?,item_tax=?,line_total=? WHERE id=?''',(*values,item_id))
                else:
                    conn.execute('''INSERT INTO invoice_items (id,invoice_id,item_name,description,quantity,unit,unit_price,tax_rate,item_tax,line_total)
                        VALUES (?,?,?,NULL,?,?,?,?,?,?)''',(item_id,row['id'],*values))
            for extra in conn.execute('SELECT id FROM invoice_items WHERE invoice_id=? AND id LIKE ?',(row['id'],f'{row["id"]}-item-%')).fetchall():
                suffix = extra['id'].rsplit('-',1)[-1]
                if suffix.isdigit() and int(suffix) > len(amounts['items']):
                    conn.execute('DELETE FROM invoice_items WHERE id=?',(extra['id'],))

            # Only the original seed-generated document records have no stored file path and a DEMO name.
            conn.execute('''UPDATE invoice_documents SET sha256=?,byte_size=?
                WHERE invoice_id=? AND stored_path IS NULL AND original_filename LIKE 'DEMO %' ''',
                (details['hash'],len(details['raw_text'].encode('utf-8')),row['id']))
            conn.execute("UPDATE payments SET amount=?,currency='USD' WHERE invoice_id=?",(amounts['total'],row['id']))

        # Refresh the 50 generated PO headers and their two base items. The mismatch invoice
        # is an extra invoice line, not an invalid PO, so PO 48 remains internally consistent.
        for n in range(1,51):
            vi = 0 if n == 49 else (n - 1) % len(VENDORS)
            vendor = VENDORS[vi]
            amount = calculate_invoice_amounts(n,vi,include_unapproved_item=False)
            po_id = f'po-demo-{n:04d}'
            po_no = f'PO-2026-{n:04d}'
            issue = conn.execute('SELECT issue_date FROM purchase_orders WHERE id=?',(po_id,)).fetchone()
            issue_date = issue['issue_date'] if issue else datetime.now(timezone.utc).date().isoformat()
            po_text = _render_po_text(n,vendor,issue_date,amount)
            conn.execute('''UPDATE purchase_orders SET po_number=?,vendor_id=?,total=?,currency='USD',source_filename=?,source_text=? WHERE id=?''',
                         (po_no,vendor['id'],amount['total'],f'DEMO PO {po_no}',po_text,po_id))
            current_items = conn.execute('SELECT id FROM purchase_order_items WHERE po_id=? ORDER BY id',(po_id,)).fetchall()
            for idx, item in enumerate(amount['items'],1):
                if idx <= len(current_items):
                    item_id = current_items[idx-1]['id']
                    conn.execute('''UPDATE purchase_order_items SET item_name=?,quantity=?,unit=?,unit_price=?,tax_rate=?,item_tax=?,line_total=? WHERE id=?''',
                                 (item['item_name'],item['quantity'],item['unit'],item['unit_price'],item['tax_rate'],item['item_tax'],item['line_total'],item_id))
                else:
                    item_id=f'{po_id}-item-{idx}'
                    conn.execute('''INSERT INTO purchase_order_items (id,po_id,item_name,quantity,unit,unit_price,tax_rate,item_tax,line_total)
                        VALUES (?,?,?,?,?,?,?,?,?)''',(item_id,po_id,item['item_name'],item['quantity'],item['unit'],item['unit_price'],item['tax_rate'],item['item_tax'],item['line_total']))

        # Re-evaluate and append migration events; earlier audit events are retained as history.
        admin = conn.execute("SELECT id,role FROM users WHERE id='usr-admin'").fetchone()
        actor = {'id':admin['id'],'role':admin['role']} if admin else {}
        for iid, old in old_hashes.items():
            result = evaluate_invoice(conn,iid)
            new = conn.execute('SELECT total,document_hash,currency FROM invoices WHERE id=?',(iid,)).fetchone()
            audit(conn,'DEMO_DATASET_MIGRATED_TO_ENGLISH_USD','SUCCESS',actor=actor,invoice_id=iid,reason='Seeded demo invoice updated to the English-language USD dataset; prior audit events were retained.',risk_level=result['risk_level'],document_hash=new['document_hash'],details={'previous_currency':old['currency'],'new_currency':new['currency'],'previous_total':old['total'],'new_total':new['total'],'previous_document_hash':old['hash'],'new_document_hash':new['document_hash']})
        audit(conn,'DEMO_DATASET_MIGRATION_COMPLETED','SUCCESS',actor=actor,reason='Updated six seeded vendors, 50 demo invoices, 50 purchase orders, and related line items to English/USD.',details={'vendors':6,'invoices':50,'purchase_orders':50,'currency':'USD','backup_file':str(backup)})

    print('Migration applied successfully. Historical audit events were preserved and new migration events appended.')
    print('Review the migrated English/USD demo records and risk findings before demonstrating the application.')
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true', help='Apply the migration after creating a timestamped database backup. Default is dry-run.')
    args = parser.parse_args()
    return migrate(apply=args.apply)


if __name__ == '__main__':
    raise SystemExit(main())
