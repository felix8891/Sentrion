from __future__ import annotations
import json, re, uuid, statistics
from datetime import datetime
from ..db import utcnow
from ..config import DEFAULT_CURRENCY
from ..currency import format_currency
from .extraction import detect_ai_directed_instruction
from .vendor_resolution import normalize_tax_id, normalize_legal_name

def normalize_name(value: str | None) -> str:
    return re.sub(r'[^a-z0-9]', '', (value or '').lower())

def evaluate_invoice(conn, invoice_id: str) -> dict:
    inv = conn.execute('SELECT * FROM invoices WHERE id=?', (invoice_id,)).fetchone()
    if not inv:
        raise ValueError('Invoice not found')
    findings = []
    run_id = str(uuid.uuid4())
    def add(code, severity, title, detail, expected=None, observed=None, source='internal_records'):
        findings.append({'finding_code': code, 'severity': severity, 'title': title, 'detail': detail,
                         'expected_value': None if expected is None else str(expected),
                         'observed_value': None if observed is None else str(observed),
                         'evidence_source': source})

    try:
        invoice_meta = json.loads(inv['extracted_json'] or '{}')
    except (TypeError, ValueError):
        invoice_meta = {}
    invoice_currency_confidence = invoice_meta.get('currency_confidence')
    if invoice_currency_confidence == 'PERSONNEL_VERIFIED':
        invoice_currency_confidence = 'HIGH'
    elif invoice_currency_confidence not in {'HIGH', 'LOW', 'POLICY'}:
        from ..currency import detect_currency
        invoice_currency_confidence = detect_currency(inv['raw_text'] or '', inv['currency'] or DEFAULT_CURRENCY)[1]
    if invoice_currency_confidence == 'LOW':
        add('CURRENCY_UNVERIFIED', 'YELLOW', 'Invoice currency needs confirmation',
            'The invoice does not contain an unambiguous currency code. The configured fallback is provisional; personnel must verify the currency before approval.',
            'Explicit currency evidence or personnel confirmation', inv['currency'] or DEFAULT_CURRENCY, 'document_content')

    # Prompt injection is a hard block for the current source document.
    injections = detect_ai_directed_instruction(inv['raw_text'] or '')
    if injections:
        add('AI_DIRECTED_INSTRUCTION', 'RED', 'Malicious instruction directed at the AI',
            'The document contains text that appears to attempt to change AI behavior or bypass security controls. The content is treated as data and is not followed.',
            'Documents must not control AI policy', '; '.join(injections), 'document_content')

    vendor = None
    identity_review = None
    if inv['vendor_id']:
        vendor = conn.execute('SELECT * FROM vendors WHERE id=?', (inv['vendor_id'],)).fetchone()
    if vendor is None and not inv['vendor_id'] and (inv['vendor_tax_id'] or inv['vendor_company_name']):
        all_vendors = conn.execute('SELECT * FROM vendors').fetchall()
        invoice_tax = normalize_tax_id(inv['vendor_tax_id'])
        if invoice_tax:
            exact_tax = [v for v in all_vendors if normalize_tax_id(v['tax_id']) == invoice_tax]
            if len(exact_tax) == 1 and exact_tax[0]['verification_status'] == 'VERIFIED':
                vendor = exact_tax[0]
            elif len(exact_tax) == 1:
                identity_review = ('UNVERIFIED_VENDOR', exact_tax[0])
            elif len(exact_tax) > 1:
                identity_review = ('AMBIGUOUS_TAX_ID', None)
        if vendor is None and identity_review is None and inv['vendor_company_name']:
            target = normalize_name(inv['vendor_company_name'])
            exact_names = [v for v in all_vendors if normalize_name(v['company_name']) == target and target]
            if exact_names:
                conflict = [v for v in exact_names if invoice_tax and normalize_tax_id(v['tax_id']) and normalize_tax_id(v['tax_id']) != invoice_tax]
                if conflict:
                    identity_review = ('TAX_ID_CONFLICT', conflict[0])
                elif len(exact_names) == 1:
                    identity_review = ('NAME_ONLY_CANDIDATE', exact_names[0])
                else:
                    identity_review = ('AMBIGUOUS_NAME', None)
    if vendor:
        if inv['vendor_id'] != vendor['id']:
            conn.execute('UPDATE invoices SET vendor_id=? WHERE id=?', (vendor['id'], invoice_id))
            inv = conn.execute('SELECT * FROM invoices WHERE id=?', (invoice_id,)).fetchone()
        if vendor['verification_status'] != 'VERIFIED':
            add('VENDOR_UNVERIFIED', 'YELLOW', 'Vendor is not verified', 'A vendor profile was found, but its verification status is not VERIFIED.', 'VERIFIED', vendor['verification_status'], 'vendor_database')
        if inv['vendor_company_name'] and normalize_name(inv['vendor_company_name']) != normalize_name(vendor['company_name']):
            add('VENDOR_NAME_MISMATCH', 'RED', 'Company name mismatch', 'The company name on the invoice differs from the trusted vendor record.', vendor['company_name'], inv['vendor_company_name'], 'vendor_database')
        if inv['vendor_tax_id'] and vendor['tax_id'] and re.sub(r'\W','', inv['vendor_tax_id']).lower() != re.sub(r'\W','', vendor['tax_id']).lower():
            add('VENDOR_TAX_ID_MISMATCH', 'RED', 'Tax ID mismatch', 'The tax ID differs from the vendor profile.', vendor['tax_id'], inv['vendor_tax_id'], 'vendor_database')
        if inv['bank_account_number']:
            verified = conn.execute("SELECT * FROM vendor_bank_accounts WHERE vendor_id=? AND account_number=? AND status='VERIFIED'", (vendor['id'], inv['bank_account_number'])).fetchone()
            if not verified:
                add('BANK_ACCOUNT_CHANGED', 'YELLOW', 'Payment destination is not verified', 'The account on the invoice does not match the currently verified vendor account. This account is new to the company records; that does not mean it was recently opened at the bank.', vendor['current_bank_account_number'], inv['bank_account_number'], 'vendor_bank_history')
            elif (verified['account_holder_name'] and inv['bank_account_holder_name']
                  and invoice_meta.get('bank_account_holder_name_source') != 'INFERRED_FROM_SUPPLIER_NAME'
                  and normalize_name(verified['account_holder_name']) != normalize_name(inv['bank_account_holder_name'])):
                add('BANK_HOLDER_MISMATCH', 'RED', 'Bank account holder mismatch', 'The account holder name on the invoice differs from the verified vendor account.', verified['account_holder_name'], inv['bank_account_holder_name'], 'vendor_bank_history')
        else:
            add('MISSING_BANK_ACCOUNT', 'YELLOW', 'Bank account number not extracted', 'The payment account number is missing or could not be extracted.', 'Verified bank account number', 'Empty', 'invoice_data')
    else:
        issue, candidate = identity_review or (None, None)
        if issue == 'TAX_ID_CONFLICT':
            add('VENDOR_TAX_ID_MISMATCH', 'RED', 'Supplier tax ID conflicts with vendor master',
                'The normalized supplier name matches a vendor candidate, but the invoice tax ID conflicts with the trusted vendor tax ID. The invoice was not automatically linked.',
                candidate['tax_id'], inv['vendor_tax_id'], 'vendor_database')
        elif issue == 'NAME_ONLY_CANDIDATE':
            add('VENDOR_IDENTITY_REVIEW', 'YELLOW', 'Supplier name match requires identity confirmation',
                'The normalized supplier name matches a vendor candidate, but an exact verified tax-ID match is unavailable. A trusted link requires authorized identity confirmation.',
                'Exact verified tax-ID match or authorized identity confirmation', inv['vendor_company_name'], 'vendor_database')
        elif issue == 'UNVERIFIED_VENDOR':
            add('VENDOR_UNVERIFIED', 'YELLOW', 'Vendor is not verified',
                'A vendor record matches the supplier tax ID, but the vendor verification status is not VERIFIED.',
                'VERIFIED', candidate['verification_status'], 'vendor_database')
        elif issue in {'AMBIGUOUS_TAX_ID', 'AMBIGUOUS_NAME'}:
            add('VENDOR_IDENTITY_REVIEW', 'YELLOW', 'Supplier identity is ambiguous',
                'Multiple vendor records match the extracted supplier identity. No trusted vendor link was created.',
                'One unique trusted vendor identity', inv['vendor_company_name'] or inv['vendor_tax_id'], 'vendor_database')
        else:
            sev = 'RED' if inv['bank_account_holder_name'] and inv['vendor_company_name'] and normalize_name(inv['bank_account_holder_name']) != normalize_name(inv['vendor_company_name']) else 'YELLOW'
            add('VENDOR_UNVERIFIED', sev, 'Vendor not found in the trusted database', 'The vendor identity on the invoice does not match a trusted profile. Do not infer legitimacy from the document name alone.', 'Verified vendor', inv['vendor_company_name'], 'vendor_database')

    # Required fields and amounts.
    required = [('invoice_number', 'Invoice number'), ('vendor_company_name', 'Vendor name'), ('total', 'Invoice total'), ('due_date', 'Due date')]
    missing = [label for key, label in required if not inv[key]]
    if missing:
        add('MISSING_REQUIRED_FIELD', 'YELLOW', 'Invoice data is incomplete', 'The following data is missing or could not be extracted: ' + ', '.join(missing), 'All required fields', ', '.join(missing), 'invoice_data')

    # Arithmetic checks based on structured items.
    item_rows = conn.execute('SELECT * FROM invoice_items WHERE invoice_id=?', (invoice_id,)).fetchall()
    if item_rows and inv['subtotal'] is not None:
        # Subtotal must be compared against a net item base, not line totals that may
        # already include tax or another amount. When every item's discount is known,
        # compute net = sum(qty × unit price - line discount). Otherwise preserve the
        # original conservative gross calculation for legacy records with no discounts.
        declared = float(inv['subtotal'])
        complete_base = all(it['quantity'] is not None and it['unit_price'] is not None for it in item_rows)
        all_printed_amounts = all(it['line_total'] is not None for it in item_rows)
        all_discounts_known = complete_base and all(it['item_discount'] is not None for it in item_rows)
        currency_code = (inv['currency'] or DEFAULT_CURRENCY).upper()
        comparison_tolerance = max(100.0, declared * 0.02) if currency_code == 'IDR' else max(0.01, declared * 0.02)
        printed_sum = sum(float(it['line_total']) for it in item_rows) if all_printed_amounts else None
        if printed_sum is not None and abs(printed_sum - declared) <= comparison_tolerance:
            computed = printed_sum
            formula = 'Sum of printed line amounts (Σ line total)'
        elif all_discounts_known:
            computed = sum(float(it['quantity']) * float(it['unit_price']) - float(it['item_discount']) for it in item_rows)
            formula = 'Net line subtotal = Σ(quantity × unit price − line discount)'
        elif complete_base:
            computed = sum(float(it['quantity']) * float(it['unit_price']) for it in item_rows)
            formula = 'Gross line subtotal = Σ(quantity × unit price); line discount details unavailable'
        else:
            computed = None
            formula = None
        if computed is not None:
            currency_code = (inv['currency'] or DEFAULT_CURRENCY).upper()
            subtotal_tolerance = max(100.0, declared * 0.02) if currency_code == 'IDR' else max(0.01, declared * 0.02)
            if abs(computed - declared) > subtotal_tolerance:
                add('SUBTOTAL_MISMATCH', 'YELLOW', 'Subtotal differs from line items',
                    f'{formula} produces a value different from the printed subtotal. Review extracted quantities, prices, discounts, and source evidence before approval.',
                    format_currency(round(computed, 0 if currency_code in {'IDR','JPY','KRW','VND','THB'} else 2), inv['currency']),
                    format_currency(declared, inv['currency']), 'invoice_line_items')

    # Duplicate checks. Keep the original finding/history and flag both records if duplicated.
    if inv['invoice_number'] and inv['vendor_id']:
        dupes = conn.execute('''SELECT id, total FROM invoices WHERE vendor_id=? AND lower(invoice_number)=lower(?) AND id<>?''', (inv['vendor_id'], inv['invoice_number'], invoice_id)).fetchall()
        if dupes:
            paid = conn.execute("SELECT 1 FROM payments WHERE invoice_id IN (%s) AND status='SIMULATED_SUCCESS' LIMIT 1" % ','.join('?' for _ in dupes), [d['id'] for d in dupes]).fetchone() if dupes else None
            sev = 'RED' if paid else 'RED'
            add('DUPLICATE_INVOICE', sev, 'Potential duplicate invoice', 'The invoice number and vendor match another recorded invoice. Ensure the bill is not paid twice.', ', '.join(d['id'] for d in dupes), invoice_id, 'invoice_history')

    # PO matching and amount discrepancy.
    po = None
    if inv['purchase_order_number']:
        po = conn.execute('SELECT * FROM purchase_orders WHERE lower(po_number)=lower(?)', (inv['purchase_order_number'],)).fetchone()
        if not po:
            add('PO_NOT_FOUND', 'YELLOW', 'Purchase Order not found', 'The PO number on the invoice does not match the database. Personnel can upload a PO or select a match manually.', inv['purchase_order_number'], 'Not found', 'purchase_order_database')
    else:
        add('PO_MISSING', 'YELLOW', 'PO reference is missing', 'The invoice does not include a PO reference. Additional verification is required under company policy.', 'PO number', 'Empty', 'invoice_data')
    if po:
        if po['vendor_id']:
            linked_po_vendor = conn.execute('SELECT * FROM vendors WHERE id=?', (po['vendor_id'],)).fetchone()
            if linked_po_vendor:
                po_tax = normalize_tax_id(po['vendor_tax_id'] if 'vendor_tax_id' in po.keys() else None)
                master_tax = normalize_tax_id(linked_po_vendor['tax_id'])
                if po_tax and master_tax and po_tax != master_tax:
                    add('PO_VENDOR_TAX_ID_MISMATCH', 'RED', 'PO supplier tax ID conflicts with linked vendor',
                        'The PO is linked to a vendor, but the reliable tax IDs on the document and vendor master conflict. The link requires investigation even if an authorized override was recorded.',
                        linked_po_vendor['tax_id'], po['vendor_tax_id'], 'purchase_order_database')
                po_supplier_name = normalize_legal_name(po['vendor_company_name'] if 'vendor_company_name' in po.keys() else None)
                master_supplier_name = normalize_legal_name(linked_po_vendor['company_name'])
                if po_supplier_name and master_supplier_name and po_supplier_name != master_supplier_name:
                    add('PO_VENDOR_NAME_MISMATCH', 'RED', 'PO supplier name conflicts with linked vendor',
                        'The printed supplier name on the PO differs from the linked trusted vendor. Review the source document and vendor relationship.',
                        linked_po_vendor['company_name'], po['vendor_company_name'], 'purchase_order_database')
        po_total_status = po['total_status'] if 'total_status' in po.keys() else 'UNKNOWN'
        po_total_requires_review = po_total_status in {'MISSING', 'REVIEW_REQUIRED', 'UNKNOWN'}
        if po_total_status == 'MISSING' or po_total_status == 'UNKNOWN':
            add('PO_TOTAL_UNAVAILABLE', 'YELLOW', 'PO grand total is unavailable',
                'The PO grand total could not be extracted reliably. The displayed numeric storage placeholder must not be used for financial comparison; review the original PO.',
                'Reliable PO grand total', 'Unavailable', 'purchase_order_document')
        elif po_total_status == 'REVIEW_REQUIRED':
            add('PO_TOTAL_REVIEW_REQUIRED', 'YELLOW', 'PO grand total requires review',
                'The displayed PO total is an external-model candidate and has not been independently confirmed from deterministic document evidence. Verify the source document before matching amounts or approval.',
                'Independently verified PO grand total', format_currency(po['total'], po['currency'] or DEFAULT_CURRENCY), 'purchase_order_document')
        invoice_currency = (inv['currency'] or DEFAULT_CURRENCY).upper()
        po_currency = (po['currency'] or DEFAULT_CURRENCY).upper()
        from ..currency import detect_currency
        po_currency_confidence = detect_currency(po['source_text'] or '', po_currency)[1]
        currencies_match = invoice_currency == po_currency
        currency_evidence_verified = invoice_currency_confidence != 'LOW' and po_currency_confidence != 'LOW'
        if currencies_match and not currency_evidence_verified and invoice_currency_confidence != 'LOW':
            add('CURRENCY_UNVERIFIED', 'YELLOW', 'PO currency needs confirmation',
                'The purchase order does not contain an unambiguous currency code. Its stored currency is provisional; personnel must verify the PO currency before amount comparison.',
                'Explicit currency evidence or personnel confirmation', po_currency, 'purchase_order_database')
        if vendor and po['vendor_id'] and po['vendor_id'] != vendor['id']:
            po_vendor = conn.execute('SELECT company_name FROM vendors WHERE id=?', (po['vendor_id'],)).fetchone()
            add('PO_VENDOR_MISMATCH', 'RED', 'PO vendor mismatch', 'The PO matched by number is registered to a different vendor.', po_vendor['company_name'] if po_vendor else po['vendor_id'], vendor['company_name'], 'purchase_order_database')
        elif vendor and not po['vendor_id'] and po['vendor_match_status'] == 'IDENTITY_CONFLICT':
            add('PO_VENDOR_IDENTITY_CONFLICT', 'RED', 'PO supplier identity conflicts with the vendor master',
                'The PO has a supplier identity conflict, including conflicting reliable identifiers. Do not establish a trusted link until an authorized reviewer resolves the evidence.',
                vendor['company_name'], po['vendor_company_name'] or po['vendor_tax_id'], 'purchase_order_database')
        elif vendor and not po['vendor_id'] and 'vendor_company_name' in po.keys() and po['vendor_company_name']:
            if normalize_name(po['vendor_company_name']) != normalize_name(inv['vendor_company_name'] or vendor['company_name']):
                add('PO_VENDOR_IDENTITY_MISMATCH', 'RED', 'Supplier identity differs between invoice and PO',
                    'The extracted supplier name on the PO differs from the invoice vendor. The PO has no trusted vendor link; verify identity before approval.',
                    vendor['company_name'], po['vendor_company_name'], 'purchase_order_document')
            else:
                add('PO_VENDOR_LINK_REVIEW', 'YELLOW', 'PO supplier is not linked to the trusted vendor',
                    'The extracted supplier name matches the invoice name, but the PO has no persisted link to a trusted vendor record. Verify the vendor identity and link before approval.',
                    vendor['company_name'], po['vendor_company_name'], 'purchase_order_document')
        if not currencies_match:
            add('CURRENCY_MISMATCH', 'RED', 'Invoice and PO currencies differ',
                f'The invoice is denominated in {invoice_currency}, while the matched PO is denominated in {po_currency}. Monetary totals and unit prices are not compared until the currency discrepancy is resolved; no exchange-rate conversion is applied.',
                po_currency, invoice_currency, 'purchase_order_database')
        elif currency_evidence_verified and inv['total'] is not None and not po_total_requires_review:
            delta = float(inv['total']) - float(po['total'])
            if invoice_currency == 'IDR':
                threshold = max(500_000.0, abs(float(po['total'])) * 0.10)
                threshold_detail = 'For IDR, a difference above the greater of 10% of the PO total or IDR 500,000 is a demo RED threshold.'
                variance_tolerance = 100.0
            else:
                threshold = abs(float(po['total'])) * 0.10
                threshold_detail = f'For {invoice_currency}, a difference above 10% of the PO total is a demo RED threshold.'
                variance_tolerance = 0.01
            if abs(delta) > threshold:
                # A RED PO mismatch can only be exception-reviewed by two different people with evidence.
                # The exception is bound to exact amounts and currencies that were reviewed.
                accepted_exception = False
                approved = conn.execute("SELECT context_json FROM approval_requests WHERE request_type='RED_RESOLUTION' AND entity_type='invoice' AND entity_id=? AND status='APPROVED' ORDER BY approved_at DESC", (invoice_id,)).fetchall()
                for approved_row in approved:
                    try:
                        context = json.loads(approved_row['context_json'] or '{}')
                        codes = context.get('resolved_finding_codes', [])
                        same_total = abs(float(context.get('invoice_total', -999999999)) - float(inv['total'])) < 0.01
                        same_po = abs(float(context.get('po_total', -999999999)) - float(po['total'])) < 0.01
                        same_currencies = context.get('invoice_currency') == invoice_currency and context.get('po_currency') == po_currency
                        if 'PO_MISMATCH' in codes and same_total and same_po and same_currencies:
                            accepted_exception = True
                            break
                    except Exception:
                        continue
                if not accepted_exception:
                    add('PO_MISMATCH', 'RED', 'Invoice total differs significantly from the PO',
                        threshold_detail + ' This is a demo rule and should be adjusted to company policy.',
                        format_currency(po['total'], po_currency), format_currency(inv['total'], invoice_currency), 'purchase_order_database')
            elif abs(delta) > variance_tolerance:
                add('PO_AMOUNT_VARIANCE', 'YELLOW', 'Invoice total differs slightly from the PO',
                    'There is a difference between the invoice and PO amounts that requires review.',
                    format_currency(po['total'], po_currency), format_currency(inv['total'], invoice_currency), 'purchase_order_database')
        po_items = conn.execute('SELECT * FROM purchase_order_items WHERE po_id=?', (po['id'],)).fetchall()
        if item_rows and po_items and not po_total_requires_review:
            invoice_by_name = {normalize_name(x['item_name']): x for x in item_rows}
            for pit in po_items:
                key = normalize_name(pit['item_name'])
                if key in invoice_by_name:
                    it = invoice_by_name[key]
                    if pit['quantity'] is not None and it['quantity'] is not None and abs(float(pit['quantity'])-float(it['quantity'])) > 0.001:
                        add('ITEM_QUANTITY_MISMATCH', 'YELLOW', 'Item quantity differs from the PO', f"Quantity for item {pit['item_name']} differs from the PO.", pit['quantity'], it['quantity'], 'purchase_order_items')
                    if currencies_match and currency_evidence_verified and pit['unit_price'] is not None and it['unit_price'] is not None:
                        exp_price=float(pit['unit_price']); obs_price=float(it['unit_price'])
                        price_tolerance = max(1000.0, abs(exp_price)*0.05) if invoice_currency == 'IDR' else max(0.01, abs(exp_price)*0.05)
                        if abs(obs_price-exp_price) > price_tolerance:
                            add('ITEM_PRICE_MISMATCH', 'YELLOW', 'Item unit price differs from the PO', f"The unit price for item {pit['item_name']} differs materially from the PO.", format_currency(exp_price, po_currency), format_currency(obs_price, invoice_currency), 'purchase_order_items')
                else:
                    add('PO_ITEM_MISSING', 'YELLOW', 'PO item missing from invoice', f"Item {pit['item_name']} on the PO was not found in the invoice line items.", pit['item_name'], 'Not found', 'purchase_order_items')

    # Amount anomaly vs prior invoices: only compare when enough history exists.
    if inv['vendor_id'] and inv['total'] is not None and invoice_currency_confidence != 'LOW':
        history = conn.execute("SELECT total FROM invoices WHERE vendor_id=? AND id<>? AND total IS NOT NULL AND processing_status='COMPLETED' AND upper(currency)=upper(?) ORDER BY invoice_receive_date DESC LIMIT 20", (inv['vendor_id'], invoice_id, (inv['currency'] or DEFAULT_CURRENCY))).fetchall()
        vals = [float(r['total']) for r in history if float(r['total'] or 0) > 0]
        if len(vals) >= 3:
            median = statistics.median(vals)
            amount = float(inv['total'])
            if median and (amount > median * 3 or amount < median * 0.30):
                add('AMOUNT_ANOMALY', 'YELLOW', 'Amount deviates from history', 'The invoice total is far outside the vendor’s historical median. The difference may be legitimate but requires review.', format_currency(round(median, 0 if (inv['currency'] or DEFAULT_CURRENCY).upper() == 'IDR' else 2), inv['currency']), format_currency(amount, inv['currency']), 'invoice_history')

    # Fail closed when processing/verification is incomplete. PARTIAL means the
    # pipeline finished but required fields still need personnel review.
    if inv['extraction_status'] == 'PARTIAL':
        missing_fields = invoice_meta.get('missing_required_fields') or missing
        missing_summary = ', '.join(str(value) for value in missing_fields) if missing_fields else 'Required fields need review'
        add('EXTRACTION_INCOMPLETE', 'YELLOW', 'Extraction needs review',
            'Document processing finished, but required fields are missing or could not be verified: ' + missing_summary + '.',
            'All required fields extracted and checked', missing_summary, 'invoice_data')
    elif inv['extraction_status'] != 'COMPLETED':
        add('EXTRACTION_INCOMPLETE', 'YELLOW', 'Extraction is incomplete',
            'The document extraction job has not completed successfully. Review the processing status and source document.',
            'COMPLETED', inv['extraction_status'], 'processing_status')

    rank = {'GREEN': 0, 'YELLOW': 1, 'RED': 2}
    level = max((f['severity'] for f in findings), key=lambda s: rank.get(s, 1), default='GREEN')
    reasons = [f['title'] for f in findings]
    now = utcnow()
    conn.execute("UPDATE risk_findings SET status='SUPERSEDED' WHERE invoice_id=? AND status='ACTIVE'", (invoice_id,))
    for f in findings:
        conn.execute('''INSERT INTO risk_findings (id, invoice_id, finding_code, severity, title, detail, expected_value,
            observed_value, evidence_source, status, analysis_run_id, created_at) VALUES (?,?,?,?,?,?,?,?,?,'ACTIVE',?,?)''',
            (str(uuid.uuid4()), invoice_id, f['finding_code'], f['severity'], f['title'], f['detail'],
             f['expected_value'], f['observed_value'], f['evidence_source'], run_id, now))
    conn.execute('UPDATE invoices SET risk_level=?, risk_reasons_json=?, updated_at=? WHERE id=?', (level, json.dumps(reasons, ensure_ascii=False), now, invoice_id))
    return {'risk_level': level, 'reasons': reasons, 'findings': findings, 'analysis_run_id': run_id}
