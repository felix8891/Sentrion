from __future__ import annotations
import json, uuid
from pathlib import Path
from ..db import get_db, utcnow
from ..config import DEFAULT_CURRENCY
from ..currency import detect_currency
from .audit import audit
from .extraction import extract_document, parse_amount, parse_number
from .risk import evaluate_invoice
from .llm_extraction import optional_llm_extract
from .vendor_resolution import classify_po_vendor_identity


def record_extraction_trace(invoice_id: str, trace: dict, processing_run_id: str, document_hash: str | None = None):
    """Persist a minimal, secret-free extraction provenance event once per processing run."""
    source = trace.get('extraction_source', 'local_ocr_regex_demo')
    request_status = trace.get('request_status', 'unknown')
    if source == 'external_llm_structured_extraction' and request_status == 'succeeded':
        event_type, event_result = 'LLM_EXTRACTION_COMPLETED', 'SUCCESS'
        reason = 'External LLM returned a valid structured extraction result.'
    elif source == 'fallback_after_llm_error':
        event_type, event_result = 'LLM_EXTRACTION_FALLBACK', 'FALLBACK'
        reason = 'External LLM failed; local extraction data was retained.'
    else:
        event_type, event_result = 'EXTRACTION_ENGINE_SELECTED', 'LOCAL'
        if request_status == 'disabled':
            reason = 'External LLM is disabled; local extraction was used.'
        elif request_status == 'api_key_missing':
            reason = 'External LLM was enabled but no API key was configured; local extraction was used.'
        else:
            reason = 'Local extraction was used for this document.'

    details = {
        'extraction_source': source,
        'provider': trace.get('provider'),
        'provider_host': trace.get('provider_host'),
        'model': trace.get('model'),
        'request_attempted': bool(trace.get('request_attempted')),
        'request_status': request_status,
        'fallback_used': bool(trace.get('fallback_used')),
        'error_type': trace.get('error_type'),
        'validated_json_object': bool(trace.get('validated_json_object')),
        'provided_field_names': trace.get('provided_field_names', []),
        'conflicting_field_names': trace.get('conflicting_field_names', []),
        'retry_count': int(trace.get('retry_count') or 0),
        'latency_ms': trace.get('latency_ms'),
        'merge_strategy': trace.get('merge_strategy'),
        'timestamp': trace.get('timestamp') or utcnow(),
        'processing_run_id': processing_run_id,
    }
    # Idempotency guard: a retry within the same processing run must not create a duplicate trace.
    run_marker = f'"processing_run_id": "{processing_run_id}"'
    with get_db() as conn:
        exists = conn.execute(
            'SELECT 1 FROM audit_events WHERE invoice_id=? AND event_type=? AND instr(details_json, ?) > 0 LIMIT 1',
            (invoice_id, event_type, run_marker),
        ).fetchone()
        if exists:
            return
        current = conn.execute('SELECT risk_level, document_hash FROM invoices WHERE id=?', (invoice_id,)).fetchone()
        audit(conn, event_type, event_result, invoice_id=invoice_id, reason=reason,
              details=details, risk_level=current['risk_level'] if current else None,
              document_hash=document_hash or (current['document_hash'] if current else None))


def process_invoice(invoice_id: str):
    processing_run_id = uuid.uuid4().hex
    with get_db() as conn:
        row = conn.execute('SELECT * FROM invoices WHERE id=?', (invoice_id,)).fetchone()
        if not row or row['processing_status'] == 'COMPLETED': return
        conn.execute("UPDATE invoices SET processing_status='PROCESSING', extraction_status='PROCESSING', updated_at=? WHERE id=?", (utcnow(), invoice_id))
        audit(conn, 'INVOICE_PROCESSING_STARTED', 'PROCESSING', invoice_id=invoice_id, reason='Document added to the processing queue', document_hash=row['document_hash'])
        file_path = row['stored_path']
        filename = row['source_filename']
        received = row['invoice_receive_date']
    try:
        if not file_path or not Path(file_path).exists():
            # Seeded documents already include structured data and use a clearly labeled demo record.
            with get_db() as conn:
                row = conn.execute('SELECT * FROM invoices WHERE id=?', (invoice_id,)).fetchone()
                if row and invoice_id.startswith('inv-demo-') and row['raw_text'] and row['extracted_json']:
                    # Only explicitly seeded demo records may be re-evaluated without their
                    # original file. A user upload with a missing file must fail visibly.
                    evaluate_invoice(conn, invoice_id)
                    conn.execute("UPDATE invoices SET extraction_status='COMPLETED', processing_status='COMPLETED', updated_at=? WHERE id=?", (utcnow(), invoice_id))
                    audit(conn, 'INVOICE_DEMO_ANALYSIS_COMPLETED', 'SUCCESS', invoice_id=invoice_id, reason='Demo data analysis saved', risk_level=conn.execute('SELECT risk_level FROM invoices WHERE id=?',(invoice_id,)).fetchone()['risk_level'], document_hash=row['document_hash'])
                    return
            raise ValueError('The source file is unavailable for processing.')
        text, fields, items, mime = extract_document(Path(file_path), filename, received, document_type='invoice')
        local_fields_before_llm = dict(fields)
        fields, extraction_engine, extraction_trace = optional_llm_extract(text, fields, document_type='invoice')
        # The company-name fallback improves UI completeness but remains explicitly
        # inferred and must never act as independent bank-ownership evidence.
        llm_provided_fields = set(extraction_trace.get('provided_field_names') or [])
        if fields.get('bank_account_holder_name') and not local_fields_before_llm.get('bank_account_holder_name') and 'bank_account_holder_name' in llm_provided_fields:
            # Model extraction is a candidate, not independent proof that the name was printed.
            fields['bank_account_holder_name_source'] = 'EXTERNAL_LLM_CANDIDATE'
        elif not fields.get('bank_account_holder_name') and fields.get('vendor_company_name'):
            fields['bank_account_holder_name'] = fields['vendor_company_name']
            fields['bank_account_holder_name_source'] = 'INFERRED_FROM_SUPPLIER_NAME'
        elif fields.get('bank_account_holder_name'):
            fields['bank_account_holder_name_source'] = 'EXPLICIT_DOCUMENT_VALUE'
        else:
            fields['bank_account_holder_name_source'] = 'MISSING'
        fields['extraction_source'] = extraction_engine
        fields['extraction_trace'] = extraction_trace
        record_extraction_trace(invoice_id, extraction_trace, processing_run_id, row['document_hash'])
        # Currency is detected from the source document, never trusted solely from model output.
        detected_currency, currency_confidence = detect_currency(text, DEFAULT_CURRENCY)
        fields['currency'] = detected_currency
        fields['currency_confidence'] = currency_confidence
        fields['currency_source'] = 'EXPLICIT_DOCUMENT_EVIDENCE' if currency_confidence == 'HIGH' else ('CONFIGURED_USD_POLICY' if currency_confidence == 'POLICY' else 'CONFIGURED_DEFAULT_UNCONFIRMED')
        # Normalize provider output before it reaches persistence or risk rules.
        for money_key in ('subtotal','discount','taxable_amount','tax','shipping','other_charges','rounding','total'):
            value=fields.get(money_key)
            if value is not None:
                fields[money_key] = float(value) if isinstance(value,(int,float)) else parse_amount(str(value), fields.get('currency'))
        if fields.get('bank_account_number') is not None:
            fields['bank_account_number'] = ''.join(ch for ch in str(fields['bank_account_number']) if ch.isdigit()) or None
        items = fields.get('items', items) if isinstance(fields.get('items'), list) else items
        normalized_items=[]
        for item in items:
            if not isinstance(item,dict) or not item.get('item_name'):
                continue
            norm=dict(item)
            for key in ('quantity','tax_rate'):
                if norm.get(key) is not None and not isinstance(norm[key],(int,float)):
                    norm[key]=parse_number(str(norm[key]))
            for key in ('unit_price','item_tax','line_total','item_discount'):
                if norm.get(key) is not None and not isinstance(norm[key],(int,float)):
                    norm[key]=parse_amount(str(norm[key]), fields.get('currency'))
            norm.setdefault('description',None); norm.setdefault('unit',None); norm.setdefault('sku',None); norm.setdefault('item_discount',None)
            normalized_items.append(norm)
        items=normalized_items
        fields['items'] = items
        model_fields = set(extraction_trace.get('provided_field_names') or [])
        field_provenance = {}
        for field_name, value in fields.items():
            if field_name in {'field_provenance','extraction_trace','items'}:
                continue
            if field_name == 'currency':
                field_provenance[field_name] = fields.get('currency_source', 'CONFIGURED_DEFAULT_UNCONFIRMED')
            elif field_name == 'bank_account_holder_name':
                field_provenance[field_name] = fields.get('bank_account_holder_name_source', 'MISSING')
            elif value in (None, '', []):
                field_provenance[field_name] = 'MISSING'
            elif field_name in model_fields and local_fields_before_llm.get(field_name) in (None, '', []):
                field_provenance[field_name] = 'EXTERNAL_LLM_CANDIDATE'
            else:
                field_provenance[field_name] = 'DOCUMENT_TEXT'
        fields['field_provenance'] = field_provenance
        # Pipeline success and extraction completeness are distinct. The job may
        # finish successfully while required fields still need human review.
        required_fields = {
            'invoice_number': 'Invoice number',
            'vendor_company_name': 'Vendor name',
            'total': 'Invoice total',
            'due_date': 'Due date',
        }
        missing_required = [label for key, label in required_fields.items() if not fields.get(key)]
        extraction_status = 'PARTIAL' if missing_required else 'COMPLETED'
        fields['missing_required_fields'] = missing_required
        fields['extraction_status'] = extraction_status
        with get_db() as conn:
            current = conn.execute('SELECT * FROM invoices WHERE id=?', (invoice_id,)).fetchone()
            # Only a unique exact tax-ID match to a VERIFIED vendor creates an
            # automatic trusted link. Exact name-only matches remain review candidates.
            vendor_resolution = classify_po_vendor_identity(
                conn, supplier_name=fields.get('vendor_company_name'), supplier_tax_id=fields.get('vendor_tax_id'))
            vendor = vendor_resolution.get('vendor') if vendor_resolution.get('status') == 'MATCHED' else None
            conn.execute('DELETE FROM invoice_items WHERE invoice_id=?', (invoice_id,))
            vals = [
                fields.get('invoice_issue_date'), fields.get('invoice_number'), vendor['id'] if vendor else None,
                fields.get('vendor_company_name'), fields.get('vendor_address'), fields.get('vendor_tax_id'),
                fields.get('purchase_order_number'), fields.get('due_date'), fields.get('currency') or DEFAULT_CURRENCY,
                fields.get('bank_name'), fields.get('bank_account_number'), fields.get('bank_account_holder_name'),
                fields.get('subtotal'), fields.get('discount'), fields.get('tax'), fields.get('total'),
            ]
            conn.execute('''UPDATE invoices SET invoice_issue_date=?, invoice_number=?, vendor_id=?, vendor_company_name=?, vendor_address=?, vendor_tax_id=?,
                purchase_order_number=?, due_date=?, currency=?, bank_name=?, bank_account_number=?, bank_account_holder_name=?, subtotal=?, discount=?, tax=?, total=?,
                raw_text=?, extracted_json=?, extraction_status=?, processing_status='COMPLETED', updated_at=? WHERE id=?''',
                (*vals, text, json.dumps(fields, ensure_ascii=False), extraction_status, utcnow(), invoice_id))
            for line_number, it in enumerate(items, start=1):
                conn.execute('''INSERT INTO invoice_items (id, invoice_id, item_name, description, quantity, unit, unit_price, tax_rate, item_tax, line_total, sku, item_discount, item_discount_source, line_number)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)''', (str(uuid.uuid4()), invoice_id, it['item_name'], it.get('description'), it.get('quantity'), it.get('unit'), it.get('unit_price'), it.get('tax_rate'), it.get('item_tax'), it.get('line_total'), it.get('sku'), it.get('item_discount'), it.get('item_discount_source'), line_number))
            evaluate_invoice(conn, invoice_id)
            if extraction_status == 'PARTIAL':
                audit(conn, 'INVOICE_EXTRACTION_PARTIAL', 'PARTIAL', invoice_id=invoice_id,
                      reason='Document processing finished, but required fields need personnel review.',
                      risk_level=conn.execute('SELECT risk_level FROM invoices WHERE id=?',(invoice_id,)).fetchone()['risk_level'],
                      document_hash=current['document_hash'],
                      details={'mime_type': mime, 'item_count': len(items), 'extraction_engine': extraction_engine,
                               'missing_required_fields': missing_required})
            else:
                audit(conn, 'INVOICE_PROCESSING_COMPLETED', 'SUCCESS', invoice_id=invoice_id,
                      reason='Extraction and initial checks completed',
                      risk_level=conn.execute('SELECT risk_level FROM invoices WHERE id=?',(invoice_id,)).fetchone()['risk_level'],
                      document_hash=current['document_hash'],
                      details={'mime_type': mime, 'item_count': len(items), 'extraction_engine': extraction_engine,
                               'missing_required_fields': []})
    except Exception as exc:
        with get_db() as conn:
            conn.execute("UPDATE invoices SET extraction_status='FAILED', processing_status='FAILED', verification_status='PENDING', risk_level='YELLOW', risk_reasons_json=?, updated_at=? WHERE id=?", (json.dumps(['Extraction failed; manual review is required'], ensure_ascii=False), utcnow(), invoice_id))
            audit(conn, 'INVOICE_PROCESSING_FAILED', 'FAILED', invoice_id=invoice_id, reason='The document could not be processed. Check its format and readability.', details={'error_type': type(exc).__name__})

def process_invoice_batch(invoice_ids: list[str]):
    for invoice_id in invoice_ids:
        process_invoice(invoice_id)
