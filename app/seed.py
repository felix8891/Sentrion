from __future__ import annotations
import hashlib, json, uuid
from datetime import datetime, timedelta, timezone
from .db import get_db, utcnow
from .security import hash_password
from .services.risk import evaluate_invoice
from .services.audit import audit
from .demo_dataset import VENDORS, calculate_invoice_amounts, invoice_number_for_demo, invoice_text, po_number_for_demo_invoice

DEMO_PASSWORDS = {
    'finance@sentrion.local': 'FinanceDemo!2026',
    'supervisor@sentrion.local': 'SupervisorDemo!2026',
    'admin@sentrion.local': 'AdminDemo!2026',
}


def seed_demo_data():
    with get_db() as conn:
        existing = conn.execute('SELECT COUNT(*) AS n FROM invoices').fetchone()['n']
        if existing:
            return
        now = utcnow()
        users = [
            ('usr-finance', 'Dina Pratama', 'finance@sentrion.local', 'finance', DEMO_PASSWORDS['finance@sentrion.local']),
            ('usr-supervisor', 'Bima Santoso', 'supervisor@sentrion.local', 'supervisor', DEMO_PASSWORDS['supervisor@sentrion.local']),
            ('usr-admin', 'Ayu Maharani', 'admin@sentrion.local', 'admin', DEMO_PASSWORDS['admin@sentrion.local']),
        ]
        for uid, name, email, role, password in users:
            conn.execute('INSERT OR IGNORE INTO users (id,name,email,password_hash,role,is_active,created_at) VALUES (?,?,?,?,?,1,?)', (uid,name,email,hash_password(password),role,now))

        for v in VENDORS:
            conn.execute('''INSERT INTO vendors (id,vendor_code,company_name,address,tax_id,verification_status,current_bank_name,current_bank_account_number,current_bank_account_holder,created_at,updated_at)
                VALUES (?,?,?,?,?,'VERIFIED',?,?,?,?,?)''', (v['id'],v['code'],v['name'],v['address'],v['tax_id'],v['bank'],v['account'],v['name'],now,now))
            conn.execute('''INSERT INTO vendor_bank_accounts (id,vendor_id,bank_name,account_number,account_holder_name,status,created_at,verified_at,verified_by)
                VALUES (?,?,?,?,?,'VERIFIED',?,?,?)''', (str(uuid.uuid4()),v['id'],v['bank'],v['account'],v['name'],now,now,'usr-supervisor'))

        invoice_ids = []
        today = datetime.now(timezone.utc).date()
        for n in range(1, 51):
            vi = 0 if n == 49 else (n - 1) % len(VENDORS)
            vendor = VENDORS[vi]
            invoice_id = f'inv-demo-{n:04d}'
            inv_no = invoice_number_for_demo(n)
            own_po_no = f'PO-2026-{n:04d}'
            invoice_po_no = po_number_for_demo_invoice(n)
            issue_date = (today - timedelta(days=(50-n)*2+1)).isoformat()
            due_date = (today + timedelta(days=(n % 14)+1)).isoformat()
            if n == 50:
                due_date = None

            # PO amounts describe the PO's own line items. Invoice 48 adds an
            # unapproved line item which is deliberately absent from its PO.
            po_amounts = calculate_invoice_amounts(n, vi, include_unapproved_item=False)
            invoice_amounts = calculate_invoice_amounts(n, vi, include_unapproved_item=True)
            bank = vendor['bank']
            account = vendor['account']
            holder = vendor['name']
            if n == 46:
                account = '889900001234'  # Deliberately not present in the verified vendor-account table.
            malicious = n == 47
            raw_text = invoice_text(
                invoice_number=inv_no, issue_date=issue_date, due_date=due_date,
                vendor=vendor, po_number=invoice_po_no, bank_name=bank,
                account_number=account, account_holder=holder,
                amounts=invoice_amounts, malicious=malicious,
            )
            hash_value = hashlib.sha256(raw_text.encode('utf-8')).hexdigest()
            receive = (datetime.now(timezone.utc) - timedelta(days=50-n, hours=n%8)).replace(microsecond=0).isoformat()
            extracted = {
                'invoice_number': inv_no,
                'invoice_receive_date': receive,
                'invoice_issue_date': issue_date,
                'vendor_company_name': vendor['name'],
                'vendor_address': vendor['address'],
                'vendor_tax_id': vendor['tax_id'],
                'purchase_order_number': invoice_po_no,
                'due_date': due_date,
                'currency': 'USD',
                'currency_confidence': 'HIGH',
                'bank_name': bank,
                'bank_account_number': account,
                'bank_account_holder_name': holder,
                'subtotal': invoice_amounts['subtotal'],
                'discount': invoice_amounts['discount'],
                'tax': invoice_amounts['tax'],
                'total': invoice_amounts['total'],
                'items': invoice_amounts['items'],
            }
            po_items_text = '\n'.join(
                f'ITEM: {it["item_name"]} | QTY: {it["quantity"]} | UNIT: {it["unit"]} | UNIT PRICE: {it["unit_price"]} | TAX: {it["tax_rate"]}% | TOTAL: {it["line_total"]}'
                for it in po_amounts['items']
            )
            po_text = '\n'.join([
                'PURCHASE ORDER', f'Purchase Order Number: {own_po_no}', 'Currency: USD',
                f'Vendor: {vendor["name"]}', f'Vendor Address: {vendor["address"]}',
                f'Tax ID: {vendor["tax_id"]}', f'Issue Date: {issue_date}',
                f'Subtotal: {po_amounts["subtotal"]:,}', f'Tax: {po_amounts["tax"]:,}',
                f'Total: {po_amounts["total"]:,}', po_items_text,
            ])
            po_seed_fields = {
                'purchase_order_number': own_po_no,
                'invoice_issue_date': issue_date,
                'vendor_company_name': vendor['name'],
                'vendor_address': vendor['address'],
                'vendor_tax_id': vendor['tax_id'],
                'buyer_company_name': None,
                'buyer_address': None,
                'buyer_tax_id': None,
                'currency': 'USD',
                'currency_confidence': 'HIGH',
                'currency_source': 'EXPLICIT_DOCUMENT_EVIDENCE',
                'subtotal': po_amounts['subtotal'],
                'discount': po_amounts.get('discount', 0),
                'tax': po_amounts['tax'],
                'shipping': po_amounts.get('shipping', 0),
                'other_charges': 0,
                'rounding': 0,
                'total': po_amounts['total'],
                'document_type': 'purchase_order',
                'extraction_source': 'seeded_demo_data',
                'extraction_status': 'COMPLETED',
            }
            conn.execute('''INSERT INTO purchase_orders
                (id,po_number,vendor_id,issue_date,total,currency,source_filename,source_text,status,created_at,
                 vendor_company_name,vendor_address,vendor_tax_id,buyer_company_name,buyer_address,buyer_tax_id,
                 subtotal,discount,tax,shipping,other_charges,rounding,extracted_json,currency_confidence,total_status,total_source)
                VALUES (?,?,?,?,?,'USD',?,?, 'ACTIVE',?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (f'po-demo-{n:04d}',own_po_no,vendor['id'],issue_date,po_amounts['total'],f'DEMO PO {own_po_no}',po_text,now,
                 vendor['name'],vendor['address'],vendor['tax_id'],None,None,None,po_amounts['subtotal'],po_amounts.get('discount',0),
                 po_amounts['tax'],po_amounts.get('shipping',0),0,0,json.dumps(po_seed_fields,ensure_ascii=False),'HIGH','EXTRACTED','SEEDED_DEMO_DATA'))
            conn.execute("UPDATE purchase_orders SET vendor_match_status='MATCHED', vendor_match_method='SEEDED_DEMO_VENDOR_ID', vendor_match_reason='Seeded demo PO points to its configured verified vendor.', vendor_match_at=?, vendor_linked_by='usr-admin' WHERE id=?", (now, f'po-demo-{n:04d}'))
            for idx, item in enumerate(po_amounts['items'], 1):
                conn.execute('''INSERT INTO purchase_order_items (id,po_id,item_name,quantity,unit,unit_price,tax_rate,item_tax,line_total)
                    VALUES (?,?,?,?,?,?,?,?,?)''',
                    (f'po-demo-{n:04d}-item-{idx}', f'po-demo-{n:04d}', item['item_name'], item['quantity'], item['unit'], item['unit_price'], item['tax_rate'], item['item_tax'], item['line_total']))

            conn.execute('''INSERT INTO invoices (id,invoice_receive_date,invoice_issue_date,invoice_number,vendor_id,vendor_company_name,vendor_address,vendor_tax_id,
                purchase_order_number,due_date,currency,bank_name,bank_account_number,bank_account_holder_name,subtotal,discount,tax,total,extraction_status,processing_status,
                verification_status,approval_status,payment_status,risk_level,risk_reasons_json,source_filename,stored_path,document_hash,raw_text,extracted_json,created_by,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'COMPLETED','COMPLETED','PENDING','NOT_REQUESTED','NOT_EXECUTED','GREEN','[]',?,NULL,?,?,?,?,?,?)''',
                (invoice_id,receive,issue_date,inv_no,vendor['id'],vendor['name'],vendor['address'],vendor['tax_id'],invoice_po_no,due_date,'USD',bank,account,holder,
                 invoice_amounts['subtotal'],invoice_amounts['discount'],invoice_amounts['tax'],invoice_amounts['total'],f'DEMO DATA — {inv_no}',hash_value,raw_text,json.dumps(extracted,ensure_ascii=False),'usr-finance',now,now))
            for idx, item in enumerate(invoice_amounts['items'], 1):
                conn.execute('''INSERT INTO invoice_items (id,invoice_id,item_name,description,quantity,unit,unit_price,tax_rate,item_tax,line_total)
                    VALUES (?,?,?,?,?,?,?,?,?,?)''',
                    (f'{invoice_id}-item-{idx}',invoice_id,item['item_name'],None,item['quantity'],item['unit'],item['unit_price'],item['tax_rate'],item['item_tax'],item['line_total']))
            conn.execute('''INSERT INTO invoice_documents (id,invoice_id,original_filename,stored_path,sha256,byte_size,mime_type,uploaded_by,uploaded_at,is_original)
                VALUES (?,?,?,?,?,?,'text/plain','usr-admin',?,1)''', (str(uuid.uuid4()),invoice_id,f'DEMO {inv_no}.txt',None,hash_value,len(raw_text.encode()),now))
            invoice_ids.append(invoice_id)

        # Evaluate only after every invoice exists so duplicate/history comparisons are consistent.
        for iid in invoice_ids:
            evaluate_invoice(conn, iid)
            row = conn.execute('SELECT risk_level,document_hash FROM invoices WHERE id=?',(iid,)).fetchone()
            audit(conn,'DEMO_INVOICE_SEEDED','SUCCESS',invoice_id=iid,reason='Synthetic English-language USD invoice created for the local demo',risk_level=row['risk_level'],document_hash=row['document_hash'])
        audit(conn,'DEMO_DATA_SEEDED','SUCCESS',actor={'id':'usr-admin','role':'admin'},reason='Created 50 synthetic USD invoices, six vendors, and supporting purchase orders')
