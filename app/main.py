from __future__ import annotations
import json, uuid, hashlib, hmac, re, mimetypes, io, zipfile, shutil
from pathlib import Path
from contextlib import asynccontextmanager
from typing import Optional
from urllib.parse import quote

from fastapi import FastAPI, Request, Form, File, UploadFile, BackgroundTasks, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse, FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
from fastapi.templating import Jinja2Templates

from .config import APP_NAME, APP_VERSION, UPLOAD_DIR, MAX_UPLOAD_BYTES, SESSION_SECRET, SESSION_HTTPS_ONLY, EXTERNAL_AI_CONFIGURED, DEFAULT_CURRENCY
from .db import get_db, init_db, utcnow
from .currency import detect_currency, format_currency
from .security import current_user_from_request, ensure_csrf, verify_csrf, verify_password, require_roles, mask_account
from .seed import seed_demo_data
from .services.audit import audit
from .services.processing import process_invoice_batch
from .services.llm_extraction import optional_llm_extract
from .services.extraction import extract_document, extract_fields, parse_amount
from .services.risk import evaluate_invoice, normalize_name
from .services.vendor_resolution import (classify_po_vendor_identity, get_po_vendor_candidates,
    link_po_to_vendor, relink_unlinked_purchase_orders, update_unlinked_po_candidate_state, _write_po_match)
from .services.exporter import build_audit_workbook
from .ui_text import ui_text

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    seed_demo_data()
    yield

app = FastAPI(title='Sentrion MVP', version=APP_VERSION, lifespan=lifespan)
app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET, session_cookie='sentrion_session', max_age=60*60*8,
                   same_site='lax', https_only=SESSION_HTTPS_ONLY)
app.mount('/static', StaticFiles(directory=str(Path(__file__).resolve().parent / 'static')), name='static')
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent / 'templates'))
templates.env.filters['mask_account'] = mask_account
templates.env.filters['ui_text'] = ui_text
templates.env.filters['money'] = format_currency

def redirect(path: str):
    return RedirectResponse(path, status_code=303)

def user_or_redirect(request: Request):
    user = current_user_from_request(request)
    return user if user else redirect('/login')

def render(request: Request, template: str, context: dict | None = None, status_code: int = 200):
    ctx = {'request': request, 'app_name': APP_NAME, 'app_version': APP_VERSION,
           'user': current_user_from_request(request), 'csrf_token': ensure_csrf(request),
           'external_ai_configured': EXTERNAL_AI_CONFIGURED}
    if context: ctx.update(context)
    return templates.TemplateResponse(request=request, name=template, context=ctx, status_code=status_code)

def check_csrf_or_400(request: Request, token: str | None):
    if not verify_csrf(request, token):
        raise HTTPException(status_code=400, detail='The form security token is invalid. Reload the page and try again.')

def row_dict(row):
    return dict(row) if row is not None else None

def safe_json(value, fallback=None):
    try: return json.loads(value or 'null')
    except Exception: return fallback if fallback is not None else []

def payload_fingerprint(payload: dict) -> str:
    """Stable digest to bind a pending maker-checker request to the reviewed payload."""
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(canonical.encode('utf-8')).hexdigest()

def context_fingerprint_matches(context: dict) -> bool:
    expected = context.get('payload_sha256')
    if not isinstance(expected, str):
        return False
    payload = {k: v for k, v in context.items() if k != 'payload_sha256'}
    return hmac.compare_digest(expected, payload_fingerprint(payload))

def vendor_duplicate_flags(conn, *, company_name: str, tax_id: str | None = None, account_number: str | None = None,
                           exclude_vendor_id: str | None = None) -> list[str]:
    """Return warning codes, not proof of fraud or a blanket duplicate rejection."""
    flags = []
    normalized_name = normalize_name(company_name or '')
    for row in conn.execute('SELECT id, company_name, tax_id FROM vendors').fetchall():
        if exclude_vendor_id and row['id'] == exclude_vendor_id:
            continue
        if normalized_name and normalize_name(row['company_name'] or '') == normalized_name:
            flags.append('POSSIBLE_DUPLICATE_VENDOR_NAME')
            break
    if tax_id and conn.execute('SELECT 1 FROM vendors WHERE tax_id=?' + (' AND id<>?' if exclude_vendor_id else '') + ' LIMIT 1',
                               (tax_id, exclude_vendor_id) if exclude_vendor_id else (tax_id,)).fetchone():
        flags.append('TAX_ID_ALREADY_REGISTERED')
    if account_number:
        query = '''SELECT 1 FROM vendor_bank_accounts WHERE upper(replace(replace(account_number,' ',''),'-',''))=? AND status='VERIFIED' '''
        args: tuple = (re.sub(r'[\s-]', '', account_number).upper(),)
        if exclude_vendor_id:
            query += ' AND vendor_id<>?'
            args += (exclude_vendor_id,)
        if conn.execute(query + ' LIMIT 1', args).fetchone():
            flags.append('BANK_ACCOUNT_ALREADY_USED_BY_ANOTHER_VENDOR')
    return flags

def prepare_audit_rows(rows):
    """Decode safe extraction provenance for audit UI without exposing raw document text or secrets."""
    source_labels = {
        'external_llm_structured_extraction': 'External LLM structured extraction',
        'local_ocr_regex_demo': 'Local OCR / regex parser',
        'fallback_after_llm_error': 'Local fallback after LLM error',
    }
    status_labels = {
        'succeeded': 'Succeeded',
        'failed': 'Failed',
        'in_progress': 'In progress',
        'disabled': 'Disabled',
        'api_key_missing': 'API key missing',
    }
    prepared = []
    for row in rows:
        item = dict(row)
        details = safe_json(item.get('details_json'), {})
        if not isinstance(details, dict):
            details = {}
        source = details.get('extraction_source')
        if source in source_labels:
            item['extraction_trace'] = {
                'source': source_labels[source],
                'provider': details.get('provider') or 'Not identified',
                'provider_host': details.get('provider_host'),
                'model': details.get('model') or 'Not configured',
                'request_attempted': bool(details.get('request_attempted')),
                'request_status': status_labels.get(details.get('request_status'), str(details.get('request_status') or 'Unknown')),
                'fallback_used': bool(details.get('fallback_used')),
                'error_type': details.get('error_type'),
                'provided_field_names': details.get('provided_field_names', []),
            }
        else:
            item['extraction_trace'] = None
        prepared.append(item)
    return prepared

VENDOR_EVIDENCE_MIME = {
    '.pdf': 'application/pdf', '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
    '.webp': 'image/webp', '.docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    '.doc': 'application/msword', '.xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    '.xls': 'application/vnd.ms-excel', '.txt': 'text/plain', '.csv': 'text/csv',
}


def _safe_evidence_filename(value: str) -> str:
    # Treat client-provided names as display metadata only, never as storage paths.
    name = (value or 'evidence').replace('\\', '/').split('/')[-1]
    name = re.sub(r'[\x00-\x1f\x7f<>:"|?*]', '_', name).strip(' .')
    return (name or 'evidence')[:180]


def _validate_evidence_content(ext: str, data: bytes) -> None:
    if not data:
        raise HTTPException(400, 'Evidence files cannot be empty.')
    if ext == '.pdf' and not data.startswith(b'%PDF-'):
        raise HTTPException(400, 'The uploaded file does not have a valid PDF signature.')
    if ext == '.png' and not data.startswith(b'\x89PNG\r\n\x1a\n'):
        raise HTTPException(400, 'The uploaded file does not have a valid PNG signature.')
    if ext in {'.jpg', '.jpeg'} and not data.startswith(b'\xff\xd8\xff'):
        raise HTTPException(400, 'The uploaded file does not have a valid JPEG signature.')
    if ext == '.webp' and not (data.startswith(b'RIFF') and len(data) >= 12 and data[8:12] == b'WEBP'):
        raise HTTPException(400, 'The uploaded file does not have a valid WebP signature.')
    if ext in {'.docx', '.xlsx'}:
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                names = set(archive.namelist())
                total_uncompressed = sum(x.file_size for x in archive.infolist())
                if total_uncompressed > 100 * 1024 * 1024 or len(names) > 5000:
                    raise HTTPException(400, 'The Office document has an unsafe internal size or structure.')
                required = '[Content_Types].xml' in names and ('word/document.xml' in names if ext == '.docx' else 'xl/workbook.xml' in names)
                if not required:
                    raise HTTPException(400, 'The uploaded file is not a valid DOCX/XLSX document.')
        except zipfile.BadZipFile:
            raise HTTPException(400, 'The uploaded Office document is not a valid ZIP-based document.')
    if ext in {'.doc', '.xls'} and not data.startswith(bytes.fromhex('D0CF11E0A1B11AE1')):
        raise HTTPException(400, 'The uploaded legacy Office file does not have a valid document signature.')
    if ext in {'.txt', '.csv'}:
        if b'\x00' in data[:8192]:
            raise HTTPException(400, 'Text evidence contains binary data.')
        try:
            data[:8192].decode('utf-8-sig')
        except UnicodeDecodeError:
            raise HTTPException(400, 'Text evidence must be UTF-8 encoded.')


def prepare_evidence_uploads(files: list[UploadFile] | None, category: str) -> list[dict]:
    prepared = []
    for upload in files or []:
        if not upload or not upload.filename:
            continue
        if len(prepared) >= 5:
            raise HTTPException(400, 'Upload at most five files per evidence category.')
        filename = _safe_evidence_filename(upload.filename)
        ext = Path(filename).suffix.lower()
        if ext not in VENDOR_EVIDENCE_MIME:
            raise HTTPException(400, 'Unsupported evidence format. Use PDF, PNG, JPG/JPEG, WEBP, DOC/DOCX, XLS/XLSX, TXT, or CSV.')
        data = upload.file.read(MAX_UPLOAD_BYTES + 1)
        if len(data) > MAX_UPLOAD_BYTES:
            raise HTTPException(413, f'Each evidence file must be no larger than {MAX_UPLOAD_BYTES // (1024*1024)} MB.')
        _validate_evidence_content(ext, data)
        prepared.append({
            'id': str(uuid.uuid4()), 'category': category, 'original_filename': filename,
            'stored_filename': f"{uuid.uuid4().hex}{ext}", 'sha256': hashlib.sha256(data).hexdigest(),
            'byte_size': len(data), 'mime_type': VENDOR_EVIDENCE_MIME[ext], 'data': data,
        })
    return prepared


def persist_evidence_uploads(request_id: str, actor_id: str, uploads: list[dict], conn) -> list[dict]:
    if not uploads:
        return []
    request_dir = UPLOAD_DIR / 'vendor-evidence' / request_id
    request_dir.mkdir(parents=True, exist_ok=True)
    now = utcnow()
    public_meta = []
    for item in uploads:
        target = request_dir / item['stored_filename']
        target.write_bytes(item['data'])
        conn.execute('''INSERT INTO approval_attachments
            (id,request_id,category,original_filename,stored_filename,sha256,byte_size,mime_type,uploaded_by,uploaded_at)
            VALUES (?,?,?,?,?,?,?,?,?,?)''', (item['id'], request_id, item['category'], item['original_filename'],
            item['stored_filename'], item['sha256'], item['byte_size'], item['mime_type'], actor_id, now))
        public_meta.append({'id': item['id'], 'category': item['category'], 'filename': item['original_filename'],
                            'sha256': item['sha256'], 'byte_size': item['byte_size'], 'mime_type': item['mime_type']})
    return public_meta


def _approval_attachment_rows(conn, request_id: str) -> list[dict]:
    rows = conn.execute('SELECT * FROM approval_attachments WHERE request_id=? ORDER BY category,uploaded_at,original_filename', (request_id,)).fetchall()
    request_row = conn.execute('SELECT context_json FROM approval_requests WHERE id=?', (request_id,)).fetchone()
    context = safe_json(request_row['context_json'], {}) if request_row else {}
    expected_rows = {x.get('id'): x for x in (context.get('evidence_attachments') or []) if isinstance(x, dict)}
    results = []
    base = (UPLOAD_DIR / 'vendor-evidence' / request_id).resolve()
    for row in rows:
        item = dict(row)
        expected = expected_rows.get(item['id']) or {}
        metadata_matches = (expected.get('sha256') == item['sha256'] and expected.get('category') == item['category']
            and expected.get('filename') == item['original_filename'] and expected.get('byte_size') == item['byte_size']
            and expected.get('mime_type') == item['mime_type'])
        path = (base / item['stored_filename']).resolve()
        try:
            path.relative_to(base)
            data = path.read_bytes()
            valid = metadata_matches and len(data) == item['byte_size'] and hmac.compare_digest(hashlib.sha256(data).hexdigest(), item['sha256'])
        except (OSError, ValueError):
            valid = False
        item['integrity_valid'] = valid
        item['url'] = f"/approvals/{request_id}/evidence/{item['id']}" if valid else None
        results.append(item)
    # A deleted database attachment row must also fail closed if it is still expected by the request.
    present_ids = {x['id'] for x in results}
    for missing_id, expected in expected_rows.items():
        if missing_id not in present_ids:
            results.append({'id': missing_id, 'request_id': request_id, 'category': expected.get('category','unknown'),
                'original_filename': expected.get('filename','Missing attachment'), 'sha256': expected.get('sha256',''),
                'byte_size': expected.get('byte_size',0), 'mime_type': expected.get('mime_type','application/octet-stream'),
                'integrity_valid': False, 'url': None})
    return results


def audit_invoice_event(conn, invoice_id, actor, event_type, result, reason='', details=None, risk=None, approval_id=None, doc_hash=None):
    row = conn.execute('SELECT risk_level, document_hash FROM invoices WHERE id=?', (invoice_id,)).fetchone()
    audit(conn, event_type, result, actor=actor, invoice_id=invoice_id, reason=reason, details=details,
          risk_level=risk or (row['risk_level'] if row else None), approval_id=approval_id,
          document_hash=doc_hash or (row['document_hash'] if row else None))

def check_upload_bytes(filename: str, content_type: str | None, data: bytes):
    suffix = Path(filename).suffix.lower()
    allowed = {'.pdf', '.png', '.jpg', '.jpeg'}
    if suffix not in allowed:
        raise ValueError('Unsupported format. Use PDF, PNG, or JPEG.')
    if len(data) > MAX_UPLOAD_BYTES:
        raise ValueError(f'File size exceeds the limit of {MAX_UPLOAD_BYTES // (1024*1024)} MB.')
    if suffix == '.pdf' and not data.startswith(b'%PDF-'):
        raise ValueError('The file content does not appear to be a valid PDF.')
    if suffix in {'.png', '.jpg', '.jpeg'}:
        from PIL import Image
        import io
        try:
            with Image.open(io.BytesIO(data)) as img:
                img.verify()
        except Exception:
            raise ValueError('The image file is invalid or corrupted.')
    return {'.pdf':'application/pdf','.png':'image/png','.jpg':'image/jpeg','.jpeg':'image/jpeg'}[suffix]

def store_bytes(data: bytes, original_filename: str) -> tuple[str, str]:
    digest = hashlib.sha256(data).hexdigest()
    suffix = Path(original_filename).suffix.lower()
    dest = UPLOAD_DIR / f'{uuid.uuid4().hex}{suffix}'
    dest.write_bytes(data)
    return str(dest.resolve()), digest

def get_invoice(conn, invoice_id):
    return conn.execute('SELECT * FROM invoices WHERE id=?', (invoice_id,)).fetchone()

@app.get('/', response_class=HTMLResponse)
def root(request: Request):
    return redirect('/dashboard' if current_user_from_request(request) else '/login')

@app.get('/health')
def health():
    return {'status':'ok','app':APP_NAME,'version':APP_VERSION,'payment_mode':'mock-only'}

@app.get('/login', response_class=HTMLResponse)
def login_page(request: Request):
    if current_user_from_request(request): return redirect('/dashboard')
    return render(request, 'login.html', {'error': None})

@app.post('/login', response_class=HTMLResponse)
def login(request: Request, email: str = Form(...), password: str = Form(...), csrf_token: str = Form(...)):
    check_csrf_or_400(request, csrf_token)
    with get_db() as conn:
        row = conn.execute('SELECT * FROM users WHERE lower(email)=lower(?) AND is_active=1', (email.strip(),)).fetchone()
    if not row or not verify_password(password, row['password_hash']):
        return render(request, 'login.html', {'error':'The email or password is incorrect.'}, status_code=401)
    request.session.clear()
    request.session['user_id'] = row['id']
    ensure_csrf(request)
    return redirect('/dashboard')

@app.post('/logout')
def logout(request: Request, csrf_token: str = Form(...)):
    check_csrf_or_400(request, csrf_token)
    request.session.clear()
    return redirect('/login')

@app.get('/dashboard', response_class=HTMLResponse)
def dashboard(request: Request):
    user = user_or_redirect(request)
    if not isinstance(user, dict): return user
    with get_db() as conn:
        counts = {
            'total': conn.execute('SELECT COUNT(*) n FROM invoices').fetchone()['n'],
            'queued': conn.execute("SELECT COUNT(*) n FROM invoices WHERE processing_status IN ('QUEUED','PROCESSING')").fetchone()['n'],
            'green': conn.execute("SELECT COUNT(*) n FROM invoices WHERE risk_level='GREEN'").fetchone()['n'],
            'yellow': conn.execute("SELECT COUNT(*) n FROM invoices WHERE risk_level='YELLOW'").fetchone()['n'],
            'red': conn.execute("SELECT COUNT(*) n FROM invoices WHERE risk_level='RED'").fetchone()['n'],
            'awaiting': conn.execute("SELECT COUNT(*) n FROM invoices WHERE approval_status IN ('NOT_REQUESTED','PENDING') AND risk_level IN ('GREEN','YELLOW')").fetchone()['n'],
            'failed': conn.execute("SELECT COUNT(*) n FROM invoices WHERE processing_status='FAILED'").fetchone()['n'],
        }
        recent = conn.execute('''SELECT i.id,i.invoice_number,i.vendor_company_name,i.total,i.currency,i.risk_level,i.processing_status,i.extraction_status,i.verification_status,i.payment_status,i.invoice_receive_date
            FROM invoices i ORDER BY i.invoice_receive_date DESC LIMIT 8''').fetchall()
        events = conn.execute('''SELECT a.*, u.name actor_name FROM audit_events a LEFT JOIN users u ON u.id=a.actor_id ORDER BY a.created_at DESC LIMIT 7''').fetchall()
    return render(request, 'dashboard.html', {'counts':counts,'recent':recent,'events':events})

@app.get('/invoices', response_class=HTMLResponse)
def invoices_page(request: Request, q: str = '', risk: str = '', status: str = '', page: int = 1):
    user = user_or_redirect(request)
    if not isinstance(user, dict): return user
    page = max(1, min(page, 10000)); page_size = 25
    # Normalize enumerated filters so a manually edited URL behaves like the dropdown.
    risk = risk.strip().upper()
    status = status.strip().upper()
    clauses=[]; params=[]
    search_term = q.strip().lower()
    if search_term:
        like=f'%{search_term}%'
        clauses.append("(LOWER(COALESCE(invoice_number,'')) LIKE ? OR LOWER(COALESCE(vendor_company_name,'')) LIKE ? OR LOWER(COALESCE(vendor_id,'')) LIKE ?)")
        params.extend([like,like,like])
    if risk in {'GREEN','YELLOW','RED'}:
        clauses.append('risk_level=?'); params.append(risk)
    # The UI's status filter is explicitly a verification-status filter. Do not
    # OR together processing, verification, and payment state: those are distinct fields.
    verification_statuses = {'PENDING','IN_PROGRESS','EVIDENCE_RECORDED','VERIFIED','REJECTED'}
    if status in verification_statuses:
        clauses.append('verification_status=?'); params.append(status)
    where = ' WHERE ' + ' AND '.join(clauses) if clauses else ''
    with get_db() as conn:
        total = conn.execute('SELECT COUNT(*) n FROM invoices'+where, params).fetchone()['n']
        # A changed filter must not leave the user on a now-empty later page.
        max_page = max(1, (total + page_size - 1) // page_size)
        if page > max_page:
            page = max_page
        rows = conn.execute('SELECT * FROM invoices'+where+' ORDER BY invoice_receive_date DESC, id ASC LIMIT ? OFFSET ?', (*params,page_size,(page-1)*page_size)).fetchall()
    return render(request,'invoices.html',{'invoices':rows,'q':q,'risk':risk,'status':status,'page':page,'page_size':page_size,'total':total,'uploaded':request.query_params.get('uploaded'),'flash_error':request.session.pop('flash_error',None),'flash_success':request.session.pop('flash_success',None)})

@app.post('/invoices/upload')
async def upload_invoices(request: Request, background_tasks: BackgroundTasks, csrf_token: str = Form(...), files: list[UploadFile] = File(...)):
    user = user_or_redirect(request)
    if not isinstance(user, dict): return user
    check_csrf_or_400(request, csrf_token)
    if not require_roles(user,'finance','supervisor','admin'):
        raise HTTPException(status_code=403, detail='Your role is not permitted to upload invoices.')
    if not files:
        return redirect('/invoices')
    invoice_ids=[]; errors=[]
    for up in files:
        try:
            data = await up.read(MAX_UPLOAD_BYTES + 1)
            mime = check_upload_bytes(up.filename or 'invoice.pdf', up.content_type, data)
            path, digest = store_bytes(data, up.filename or 'invoice.pdf')
            iid = 'inv-' + uuid.uuid4().hex[:16]
            now = utcnow()
            with get_db() as conn:
                conn.execute('''INSERT INTO invoices (id,invoice_receive_date,currency,extraction_status,processing_status,verification_status,approval_status,payment_status,
                    risk_level,risk_reasons_json,source_filename,stored_path,document_hash,created_by,created_at,updated_at)
                    VALUES (?,?,?,'QUEUED','QUEUED','PENDING','NOT_REQUESTED','NOT_EXECUTED','YELLOW','[]',?,?,?,?,?,?)''',
                    (iid,now,DEFAULT_CURRENCY,Path(up.filename or 'invoice.pdf').name,path,digest,user['id'],now,now))
                conn.execute('''INSERT INTO invoice_documents (id,invoice_id,original_filename,stored_path,sha256,byte_size,mime_type,uploaded_by,uploaded_at,is_original)
                    VALUES (?,?,?,?,?,?,?,?,?,1)''', (str(uuid.uuid4()),iid,Path(up.filename or 'invoice.pdf').name,path,digest,len(data),mime,user['id'],now))
                audit_invoice_event(conn,iid,user,'INVOICE_RECEIVED','QUEUED','Invoice received and added to the queue.',{'filename':Path(up.filename or 'invoice.pdf').name,'byte_size':len(data)})
            invoice_ids.append(iid)
        except ValueError as exc:
            errors.append(f'{up.filename or "file"}: {exc}')
        finally:
            await up.close()
    if invoice_ids:
        background_tasks.add_task(process_invoice_batch, invoice_ids)
    if errors:
        request.session['flash_error'] = ' | '.join(errors[:3])
    return redirect(f'/invoices?uploaded={len(invoice_ids)}')

@app.get('/invoices/{invoice_id}', response_class=HTMLResponse)
def invoice_detail(request: Request, invoice_id: str):
    user = user_or_redirect(request)
    if not isinstance(user, dict): return user
    with get_db() as conn:
        inv = get_invoice(conn, invoice_id)
        if not inv: raise HTTPException(404, 'Invoice not found')
        items = conn.execute('SELECT * FROM invoice_items WHERE invoice_id=? ORDER BY COALESCE(line_number, 2147483647), id', (invoice_id,)).fetchall()
        findings = conn.execute("SELECT * FROM risk_findings WHERE invoice_id=? AND status='ACTIVE' ORDER BY CASE severity WHEN 'RED' THEN 0 WHEN 'YELLOW' THEN 1 ELSE 2 END, created_at", (invoice_id,)).fetchall()
        history_findings = conn.execute("SELECT * FROM risk_findings WHERE invoice_id=? AND status<>'ACTIVE' ORDER BY created_at DESC LIMIT 12", (invoice_id,)).fetchall()
        audits = conn.execute('''SELECT a.*,u.name actor_name FROM audit_events a LEFT JOIN users u ON u.id=a.actor_id WHERE a.invoice_id=? ORDER BY a.created_at DESC, a.rowid DESC LIMIT 20''', (invoice_id,)).fetchall()
        audits = prepare_audit_rows(audits)
        vendor = conn.execute('SELECT * FROM vendors WHERE id=?',(inv['vendor_id'],)).fetchone() if inv['vendor_id'] else None
        po_rows = conn.execute('''SELECT p.*,v.company_name AS trusted_vendor_name,v.verification_status AS trusted_vendor_status
            FROM purchase_orders p LEFT JOIN vendors v ON v.id=p.vendor_id
            WHERE p.status=? ORDER BY p.po_number DESC''',('ACTIVE',)).fetchall()
        pos=[]
        invoice_vendor_norm=normalize_name(inv['vendor_company_name'] or '')
        for po_row in po_rows:
            option=dict(po_row)
            doc_vendor_norm=normalize_name(po_row['vendor_company_name'] or '')
            trusted_vendor_norm=normalize_name(po_row['trusted_vendor_name'] or '')
            if po_row['vendor_id'] and trusted_vendor_norm:
                option['selection_supplier_label'] = po_row['trusted_vendor_name']
                option['selection_match_state'] = 'Trusted vendor linked'
            elif doc_vendor_norm and invoice_vendor_norm and doc_vendor_norm == invoice_vendor_norm:
                option['selection_supplier_label'] = po_row['vendor_company_name']
                option['selection_match_state'] = 'Invoice/PO supplier names match; vendor verification pending'
            elif po_row['vendor_company_name']:
                option['selection_supplier_label'] = po_row['vendor_company_name']
                option['selection_match_state'] = 'Supplier unverified or identity differs'
            else:
                option['selection_supplier_label'] = 'Supplier identity unavailable'
                option['selection_match_state'] = 'Manual review required'
            option['selection_total_label'] = (format_currency(po_row['total'], po_row['currency']) if po_row['total_status'] not in {'MISSING','REVIEW_REQUIRED'} else ('Grand total unavailable' if po_row['total_status']=='MISSING' else 'AI total candidate — review'))
            pos.append(option)
        requests = conn.execute("SELECT * FROM approval_requests WHERE (entity_type='invoice' AND entity_id=?) OR (entity_type='vendor' AND context_json LIKE ?) ORDER BY created_at DESC",(invoice_id, '%\"invoice_id\": \"'+invoice_id+'\"%')).fetchall()
        documents = conn.execute('SELECT * FROM invoice_documents WHERE invoice_id=? ORDER BY uploaded_at',(invoice_id,)).fetchall()
    currency_meta = (safe_json(inv['extracted_json'], {}) if inv['extracted_json'] else {}) or {}
    return render(request,'invoice_detail.html',{'invoice':inv,'currency_confidence':currency_meta.get('currency_confidence'),'extracted_data':currency_meta,'items':items,'findings':findings,'history_findings':history_findings,'audits':audits,'vendor':vendor,'purchase_orders':pos,'approval_requests':requests,'documents':documents,'flash_error':request.session.pop('flash_error',None),'flash_success':request.session.pop('flash_success',None)})

EDITABLE_FIELDS = {
    'invoice_number':str,'invoice_issue_date':str,'vendor_company_name':str,'vendor_address':str,'vendor_tax_id':str,
    'purchase_order_number':str,'due_date':str,'bank_name':str,'bank_account_number':str,'bank_account_holder_name':str,
    'subtotal':float,'discount':float,'tax':float,'total':float,'currency':str,
}

@app.post('/invoices/{invoice_id}/correct')
def correct_invoice(request: Request, invoice_id: str, csrf_token: str = Form(...), field_name: str = Form(...), new_value: str = Form(...), reason: str = Form('')):
    user = user_or_redirect(request)
    if not isinstance(user,dict): return user
    check_csrf_or_400(request,csrf_token)
    if not require_roles(user,'finance','supervisor','admin'): raise HTTPException(403,'You are not authorized to correct invoices.')
    if field_name not in EDITABLE_FIELDS: raise HTTPException(400,'This field cannot be edited through invoice correction.')
    with get_db() as conn:
        inv=get_invoice(conn,invoice_id)
        if not inv: raise HTTPException(404,'Invoice not found')
        old=inv[field_name]
        raw = new_value.strip()
        if field_name in {'subtotal','discount','tax','total'}:
            value = parse_amount(raw, inv['currency']) if raw else None
            if raw and value is None: raise HTTPException(400,'The amount is invalid.')
            if value is not None and value < 0: raise HTTPException(400,'The amount cannot be negative.')
        elif field_name == 'currency':
            value = raw.upper()
            if not re.fullmatch(r'[A-Z]{3}', value):
                raise HTTPException(400,'Currency must be a three-letter currency code, such as USD or IDR.')
        elif field_name in {'invoice_issue_date','due_date'}:
            value=raw or None
            if value:
                from datetime import date
                try: date.fromisoformat(value)
                except ValueError: raise HTTPException(400,'The date must use the YYYY-MM-DD format.')
        elif field_name=='bank_account_number':
            value=re.sub(r'\D','',raw) or None
            if value and not (5 <= len(value) <= 20): raise HTTPException(400,'The bank account number is invalid.')
        else:
            value=raw or None
        if str(old or '') == str(value or ''):
            request.session['flash_success']='No changes were made because the new value matches the existing value.'
            return redirect(f'/invoices/{invoice_id}')
        if field_name == 'currency':
            metadata = safe_json(inv['extracted_json'], {}) if inv['extracted_json'] else {}
            metadata['currency'] = value
            metadata['currency_confidence'] = 'PERSONNEL_VERIFIED'
            conn.execute('UPDATE invoices SET currency=?, extracted_json=?, updated_at=? WHERE id=?',(value,json.dumps(metadata,ensure_ascii=False),utcnow(),invoice_id))
        else:
            conn.execute(f'UPDATE invoices SET {field_name}=?, updated_at=? WHERE id=?',(value,utcnow(),invoice_id))
        audit_invoice_event(conn,invoice_id,user,'INVOICE_FIELD_CORRECTED','SUCCESS',reason or 'Personnel corrected extracted data.',{'field':field_name,'old_value':old,'new_value':value,'reason':reason or None})
        evaluate_invoice(conn,invoice_id)
        audit_invoice_event(conn,invoice_id,user,'RISK_REEVALUATED','COMPLETED','The risk engine was rerun after data correction.',{'trigger':'manual_correction'})
    request.session['flash_success']='Correction saved and risk checks rerun.'
    return redirect(f'/invoices/{invoice_id}')

@app.post('/invoices/{invoice_id}/reanalyze')
def reanalyze_invoice(request: Request, invoice_id: str, background_tasks: BackgroundTasks, csrf_token: str = Form(...)):
    user=user_or_redirect(request)
    if not isinstance(user,dict): return user
    check_csrf_or_400(request,csrf_token)
    reprocess_document = False
    with get_db() as conn:
        inv=get_invoice(conn,invoice_id)
        if not inv: raise HTTPException(404,'Invoice not found')
        if inv['processing_status'] in {'QUEUED','PROCESSING'}:
            request.session['flash_error']='This invoice is already queued or processing. Wait for the current job to finish.'
            return redirect(f'/invoices/{invoice_id}')
        # Uploaded documents can be re-run through extraction after a parser/OCR fix.
        # Preserve audit history and do not reopen rejected or paid cases.
        source_path = Path(inv['stored_path']) if inv['stored_path'] else None
        has_original_source = bool(source_path and source_path.is_file() and not invoice_id.startswith('inv-demo-'))
        if has_original_source:
            pending_approval = conn.execute("SELECT 1 FROM approval_requests WHERE entity_type='invoice' AND entity_id=? AND status='PENDING_SECOND_APPROVER' LIMIT 1", (invoice_id,)).fetchone()
            if inv['payment_status'] != 'NOT_EXECUTED' or inv['approval_status'] in {'PENDING','APPROVED','REJECTED'} or inv['verification_status'] == 'REJECTED' or pending_approval:
                request.session['flash_error']='This invoice cannot be reprocessed because it has a payment, rejection, or active approval state. Preserve the existing case history and create a reviewed replacement if needed.'
                return redirect(f'/invoices/{invoice_id}')
            conn.execute("UPDATE invoices SET extraction_status='QUEUED', processing_status='QUEUED', verification_status='PENDING', verification_evidence=NULL, verified_by=NULL, verified_at=NULL, approval_status='NOT_REQUESTED', risk_level='YELLOW', risk_reasons_json='[]', updated_at=? WHERE id=?", (utcnow(), invoice_id))
            audit_invoice_event(conn,invoice_id,user,'INVOICE_REPROCESS_QUEUED','QUEUED',
                'Personnel explicitly requested extraction to be rerun from the original uploaded document. Prior audit history is retained.',
                {'previous_extraction_status':inv['extraction_status'], 'previous_processing_status':inv['processing_status'],
                 'previous_invoice_number':inv['invoice_number'], 'source_filename':inv['source_filename']})
            reprocess_document = True
        elif invoice_id.startswith('inv-demo-'):
            result=evaluate_invoice(conn,invoice_id)
            audit_invoice_event(conn,invoice_id,user,'RISK_REANALYZED','COMPLETED','Risk analysis was rerun for a synthetic demo invoice.',{'risk_level':result['risk_level']})
        else:
            request.session['flash_error']='The original uploaded document is unavailable, so extraction cannot be rerun. Upload the source document again; stored data was not modified.'
            return redirect(f'/invoices/{invoice_id}')
    if reprocess_document:
        background_tasks.add_task(process_invoice_batch,[invoice_id])
        request.session['flash_success']='Re-analysis has been queued. This page will refresh to show the final extraction status.'
    else:
        request.session['flash_success']='Risk analysis completed.'
    return redirect(f'/invoices/{invoice_id}')

@app.post('/invoices/{invoice_id}/record-verification')
def record_verification(request: Request, invoice_id: str, csrf_token: str = Form(...), evidence: str = Form(...), result: str = Form(...)):
    user=user_or_redirect(request)
    if not isinstance(user,dict): return user
    check_csrf_or_400(request,csrf_token)
    if not require_roles(user,'finance','supervisor','admin'): raise HTTPException(403,'You are not authorized to record verification.')
    evidence=evidence.strip()
    if len(evidence)<10: raise HTTPException(400,'Enter sufficiently clear verification evidence/notes.')
    if result not in {'CONFIRMED','UNRESOLVED','REJECTED'}: raise HTTPException(400,'The verification outcome is invalid.')
    with get_db() as conn:
        inv=get_invoice(conn,invoice_id)
        if not inv: raise HTTPException(404,'Invoice not found')
        if result=='CONFIRMED': status='EVIDENCE_RECORDED'
        elif result=='REJECTED': status='REJECTED'
        else: status='IN_PROGRESS'
        conn.execute('UPDATE invoices SET verification_status=?, verification_evidence=?, verified_by=?, verified_at=?, updated_at=? WHERE id=?',
                     (status,evidence,user['id'],utcnow(),utcnow(),invoice_id))
        audit_invoice_event(conn,invoice_id,user,'VERIFICATION_RECORDED',result,'Personnel recorded the verification outcome. This note is not an automatic verification by the bank.',{'evidence':evidence,'result':result,'verification_status':status})
        if result=='REJECTED':
            conn.execute("UPDATE invoices SET approval_status='REJECTED', payment_status='BLOCKED' WHERE id=?",(invoice_id,))
    request.session['flash_success']='Verification notes saved.'
    return redirect(f'/invoices/{invoice_id}')

@app.post('/invoices/{invoice_id}/match-po')
def match_po(request: Request, invoice_id: str, csrf_token: str = Form(...), po_id: str = Form(...)):
    user=user_or_redirect(request)
    if not isinstance(user,dict): return user
    check_csrf_or_400(request,csrf_token)
    with get_db() as conn:
        inv=get_invoice(conn,invoice_id); po=conn.execute('SELECT * FROM purchase_orders WHERE id=? AND status=\'ACTIVE\'',(po_id,)).fetchone()
        if not inv or not po: raise HTTPException(404,'Invoice or PO not found.')
        conn.execute('UPDATE invoices SET purchase_order_number=?, updated_at=? WHERE id=?',(po['po_number'],utcnow(),invoice_id))
        audit_invoice_event(conn,invoice_id,user,'PO_MATCH_CONFIRMED','SUCCESS','Personnel confirmed the PO match.',{'po_id':po_id,'po_number':po['po_number']})
        evaluate_invoice(conn,invoice_id)
    request.session['flash_success']='PO match saved and risk re-evaluated.'
    return redirect(f'/invoices/{invoice_id}')

@app.post('/invoices/{invoice_id}/approve')
def approve_green(request: Request, invoice_id: str, csrf_token: str = Form(...), reason: str = Form('')):
    user=user_or_redirect(request)
    if not isinstance(user,dict): return user
    check_csrf_or_400(request,csrf_token)
    if not require_roles(user,'finance','supervisor','admin'): raise HTTPException(403,'You are not authorized to approve invoices.')
    with get_db() as conn:
        inv=get_invoice(conn,invoice_id)
        if not inv: raise HTTPException(404,'Invoice not found')
        if inv['approval_status']=='REJECTED' or inv['verification_status']=='REJECTED':
            request.session['flash_error']='A rejected invoice cannot be approved again. Create or revise the case through the applicable process.'
            audit_invoice_event(conn,invoice_id,user,'PAYMENT_APPROVAL_BLOCKED','BLOCKED','The invoice is REJECTED and cannot be reopened through standard approval.')
            return redirect(f'/invoices/{invoice_id}')
        # Re-evaluate current facts to avoid stale GREEN approvals.
        result=evaluate_invoice(conn,invoice_id)
        inv=get_invoice(conn,invoice_id)
        if result['risk_level']!='GREEN':
            audit_invoice_event(conn,invoice_id,user,'PAYMENT_APPROVAL_BLOCKED','BLOCKED','Approval was denied because the risk classification is not GREEN.',{'current_risk':result['risk_level']})
            request.session['flash_error']='Approval blocked: the invoice is not GREEN after the latest checks.'
            return redirect(f'/invoices/{invoice_id}')
        if inv['processing_status']!='COMPLETED' or inv['extraction_status']!='COMPLETED':
            request.session['flash_error']='Approval blocked: invoice processing is not complete.'
            return redirect(f'/invoices/{invoice_id}')
        if inv['payment_status']=='SIMULATED_SUCCESS':
            request.session['flash_error']='A simulated payment for this invoice has already been recorded.'
            return redirect(f'/invoices/{invoice_id}')
        if not inv['vendor_id'] or not inv['bank_account_number'] or inv['total'] is None:
            request.session['flash_error']='Approval blocked: the vendor, bank account, or invoice total is invalid.'
            return redirect(f'/invoices/{invoice_id}')
        approved_account=conn.execute("SELECT 1 FROM vendor_bank_accounts WHERE vendor_id=? AND account_number=? AND status='VERIFIED'",(inv['vendor_id'],inv['bank_account_number'])).fetchone()
        if not approved_account:
            request.session['flash_error']='Approval blocked: the invoice account is not a verified vendor account.'
            audit_invoice_event(conn,invoice_id,user,'PAYMENT_APPROVAL_BLOCKED','BLOCKED','The bank account is not verified.',{'bank_account':'masked'})
            return redirect(f'/invoices/{invoice_id}')
        existing=conn.execute("SELECT id FROM payments WHERE invoice_id=? AND status='SIMULATED_SUCCESS' LIMIT 1",(invoice_id,)).fetchone()
        if existing:
            request.session['flash_error']='A simulated payment already exists; duplicate payment was prevented.'
            return redirect(f'/invoices/{invoice_id}')
        conn.execute("UPDATE invoices SET approval_status='APPROVED', verification_status='VERIFIED', updated_at=? WHERE id=?",(utcnow(),invoice_id))
        audit_invoice_event(conn,invoice_id,user,'GREEN_APPROVAL','APPROVED',reason or 'Personnel approved the GREEN invoice.',{'approver':user['id']})
        # Mock payment occurs only within this explicitly simulated, server-validated route.
        payment_id=str(uuid.uuid4()); idem=f'pay-{invoice_id}'
        conn.execute('''INSERT INTO payments (id,invoice_id,idempotency_key,status,simulated_reference,initiated_by,amount,currency,created_at)
            VALUES (?,?,?,'SIMULATED_SUCCESS',?,?,?, ?,?)''',(payment_id,invoice_id,idem,'SIM-'+payment_id[:8].upper(),user['id'],inv['total'],inv['currency'] or DEFAULT_CURRENCY,utcnow()))
        conn.execute("UPDATE invoices SET payment_status='SIMULATED_SUCCESS', approval_status='APPROVED', updated_at=? WHERE id=?",(utcnow(),invoice_id))
        audit_invoice_event(conn,invoice_id,user,'MOCK_PAYMENT_EXECUTED','SIMULATED_SUCCESS','Simulated payment succeeded; no real money was transferred.',{'payment_id':payment_id,'amount':inv['total'],'currency':inv['currency']})
    request.session['flash_success']='Invoice approved. The simulated payment was recorded; no real money was transferred.'
    return redirect(f'/invoices/{invoice_id}')

@app.post('/invoices/{invoice_id}/reject')
def reject_invoice(request: Request, invoice_id: str, csrf_token: str = Form(...), reason: str = Form(...)):
    user=user_or_redirect(request)
    if not isinstance(user,dict): return user
    check_csrf_or_400(request,csrf_token)
    if not require_roles(user,'finance','supervisor','admin'): raise HTTPException(403,'You are not authorized to reject invoices.')
    reason=reason.strip()
    if len(reason)<5: raise HTTPException(400,'A rejection reason is required.')
    with get_db() as conn:
        inv=get_invoice(conn,invoice_id)
        if not inv: raise HTTPException(404,'Invoice not found')
        conn.execute("UPDATE invoices SET approval_status='REJECTED', verification_status='REJECTED', payment_status='BLOCKED', updated_at=? WHERE id=?",(utcnow(),invoice_id))
        audit_invoice_event(conn,invoice_id,user,'INVOICE_REJECTED','REJECTED',reason,{'reason':reason})
    request.session['flash_success']='Invoice rejected and payment blocked.'
    return redirect(f'/invoices/{invoice_id}')

@app.post('/invoices/{invoice_id}/red-resolution/request')
def request_red_resolution(request: Request, invoice_id: str, csrf_token: str = Form(...), reason: str = Form(...), evidence: str = Form(...)):
    user=user_or_redirect(request)
    if not isinstance(user,dict): return user
    check_csrf_or_400(request,csrf_token)
    if not require_roles(user,'finance','supervisor','admin'): raise HTTPException(403,'You are not authorized to request RED resolution.')
    reason=reason.strip(); evidence=evidence.strip()
    if len(reason)<5 or len(evidence)<10: raise HTTPException(400,'A reason and verification evidence are required.')
    with get_db() as conn:
        inv=get_invoice(conn,invoice_id)
        if not inv: raise HTTPException(404,'Invoice not found')
        if inv['risk_level']!='RED': raise HTTPException(400,'This resolution request applies only to RED cases.')
        active_red=conn.execute("SELECT finding_code FROM risk_findings WHERE invoice_id=? AND status='ACTIVE' AND severity='RED'",(invoice_id,)).fetchall()
        red_codes={r['finding_code'] for r in active_red}
        if not red_codes or red_codes != {'PO_MISMATCH'}:
            request.session['flash_error']='A multi-step RED resolution is available only for a PO amount mismatch verified with independent evidence. Identity conflicts, duplicates, and malicious AI instructions cannot be overridden directly; resolve the root cause or use a clean replacement invoice.'
            audit_invoice_event(conn,invoice_id,user,'RED_RESOLUTION_REQUEST_BLOCKED','BLOCKED','This type of RED finding is not eligible for an exception.',{'red_findings':sorted(red_codes)})
            return redirect(f'/invoices/{invoice_id}')
        pending=conn.execute("SELECT id FROM approval_requests WHERE request_type='RED_RESOLUTION' AND entity_id=? AND status='PENDING_SECOND_APPROVER'",(invoice_id,)).fetchone()
        if pending: raise HTTPException(409,'A RED resolution request is already awaiting second approval.')
        po=conn.execute('SELECT total, currency FROM purchase_orders WHERE po_number=?',(inv['purchase_order_number'],)).fetchone()
        if not po or inv['total'] is None:
            raise HTTPException(409,'Invoice and PO amounts must be available before an exception can be requested.')
        if (inv['currency'] or DEFAULT_CURRENCY).upper() != (po['currency'] or DEFAULT_CURRENCY).upper():
            raise HTTPException(409,'Invoice and PO currencies differ. Currency mismatches cannot be resolved as a PO amount exception.')
        rid=str(uuid.uuid4()); now=utcnow()
        context={'invoice_id':invoice_id,'resolved_finding_codes':['PO_MISMATCH'],'invoice_total':float(inv['total']),'po_total':float(po['total']),'invoice_currency':(inv['currency'] or DEFAULT_CURRENCY).upper(),'po_currency':(po['currency'] or DEFAULT_CURRENCY).upper(),'po_number':inv['purchase_order_number']}
        conn.execute('''INSERT INTO approval_requests (id,request_type,entity_type,entity_id,status,requested_by,reason,evidence,context_json,created_at,updated_at)
            VALUES (?,'RED_RESOLUTION','invoice',?,'PENDING_SECOND_APPROVER',?,?,?,?,?,?)''',(rid,invoice_id,user['id'],reason,evidence,json.dumps(context,ensure_ascii=False),now,now))
        audit_invoice_event(conn,invoice_id,user,'RED_RESOLUTION_REQUESTED','PENDING_SECOND_APPROVER',reason,{'evidence':evidence},approval_id=rid)
    request.session['flash_success']='RED resolution request created. The case remains blocked until the second review and approval are completed.'
    return redirect(f'/invoices/{invoice_id}')

@app.post('/vendors/{vendor_id}/bank-change/request')
def request_bank_change(request: Request, vendor_id: str, csrf_token: str = Form(...), bank_name: str = Form(...), account_number: str = Form(...), account_holder: str = Form(...), reason: str = Form(...), evidence: str = Form(...), invoice_id: str = Form(''), bank_country: str = Form(''), swift_bic: str = Form('')):
    user=user_or_redirect(request)
    if not isinstance(user,dict): return user
    check_csrf_or_400(request,csrf_token)
    if not require_roles(user,'finance','supervisor','admin'): raise HTTPException(403,'You are not authorized to request a bank-account change.')
    account_number=re.sub(r'[\s-]','',account_number).upper()
    if not bank_name.strip() or not account_holder.strip() or not re.fullmatch(r'[A-Z0-9]{5,34}',account_number): raise HTTPException(400,'Bank details and account number must be valid for a supported local-account or IBAN format.')
    reason=reason.strip(); evidence=evidence.strip()
    if len(reason)<5 or len(evidence)<10: raise HTTPException(400,'A reason and independent verification evidence are required.')
    with get_db() as conn:
        vendor=conn.execute('SELECT * FROM vendors WHERE id=?',(vendor_id,)).fetchone()
        if not vendor: raise HTTPException(404,'Vendor not found.')
        if invoice_id:
            related_invoice=conn.execute('SELECT vendor_id FROM invoices WHERE id=?',(invoice_id,)).fetchone()
            if not related_invoice or related_invoice['vendor_id'] != vendor_id:
                raise HTTPException(400,'The related invoice was not found or does not match this vendor.')
        if account_number==vendor['current_bank_account_number']:
            raise HTTPException(400,'The account number matches the active account.')
        pending=conn.execute("SELECT id FROM approval_requests WHERE request_type='BANK_ACCOUNT_CHANGE' AND entity_type='vendor' AND entity_id=? AND status='PENDING_SECOND_APPROVER' LIMIT 1",(vendor_id,)).fetchone()
        if pending:
            raise HTTPException(409,'This vendor already has a bank-account change awaiting second approval.')
        rid=str(uuid.uuid4()); now=utcnow()
        context={'bank_name':bank_name.strip(),'account_number':account_number,'account_holder':account_holder.strip(),
                 'bank_country':bank_country.strip() or None,'swift_bic':swift_bic.strip().upper() or None,
                 'invoice_id':invoice_id or None,'old_account_number':vendor['current_bank_account_number']}
        conn.execute('''INSERT INTO approval_requests (id,request_type,entity_type,entity_id,status,requested_by,reason,evidence,context_json,created_at,updated_at)
            VALUES (?,'BANK_ACCOUNT_CHANGE','vendor',?,'PENDING_SECOND_APPROVER',?,?,?,?,?,?)''',(rid,vendor_id,user['id'],reason,evidence,json.dumps(context,ensure_ascii=False),now,now))
        audit(conn,'VENDOR_BANK_CHANGE_REQUESTED','PENDING_SECOND_APPROVER',actor=user,invoice_id=invoice_id or None,reason=reason,details={'vendor_id':vendor_id,'old_account_masked':mask_account(vendor['current_bank_account_number']),'new_account_masked':mask_account(account_number),'evidence':evidence},approval_id=rid)
    request.session['flash_success']='The bank-account change is awaiting review by a second person. The new account is not yet active.'
    return redirect(f'/invoices/{invoice_id}' if invoice_id else f'/vendors/{vendor_id}')

@app.post('/approvals/{request_id}/decision')
def approval_decision(request: Request, request_id: str, csrf_token: str = Form(...), decision: str = Form(...), reason: str = Form(''), review_confirmed: str = Form('')):
    user=user_or_redirect(request)
    if not isinstance(user,dict): return user
    check_csrf_or_400(request,csrf_token)
    if decision not in {'APPROVE','REJECT'}: raise HTTPException(400,'Invalid decision.')
    multi_stage_types={'VENDOR_ONBOARDING','VENDOR_PROFILE_CHANGE'}
    with get_db() as conn:
        ar=conn.execute('SELECT * FROM approval_requests WHERE id=?',(request_id,)).fetchone()
        if not ar: raise HTTPException(404,'Approval request not found.')
        req_type=ar['request_type']
        context=safe_json(ar['context_json'],{})
        now=utcnow()

        # Vendor onboarding/profile changes require two independent checkers in addition to the maker.
        if req_type in multi_stage_types and ar['status']=='PENDING_FIRST_APPROVER':
            if not require_roles(user,'finance','supervisor','admin'):
                raise HTTPException(403,'You are not authorized to perform the first vendor review.')
            if ar['requested_by']==user['id']:
                raise HTTPException(403,'The requester cannot review or approve their own request.')
            if not context_fingerprint_matches(context):
                raise HTTPException(409,'The submitted request payload changed after submission. Cancel it and submit a new request.')
            if req_type in {'VENDOR_ONBOARDING','VENDOR_PROFILE_CHANGE'}:
                if review_confirmed.lower() not in {'1','true','yes','on'}:
                    raise HTTPException(400, 'Confirm that you reviewed the vendor details and supporting evidence before recording a decision.')
                reviewed_state = request.session.get('approval_reviewed_requests', {})
                expected_review_fingerprint = context.get('payload_sha256') or payload_fingerprint(context)
                if reviewed_state.get(request_id) != expected_review_fingerprint:
                    raise HTTPException(409, 'Open Review Details and inspect the current request before recording a decision.')
            if req_type == 'VENDOR_ONBOARDING' and decision == 'APPROVE':
                category_rows = conn.execute('SELECT category,COUNT(*) n FROM approval_attachments WHERE request_id=? GROUP BY category',(request_id,)).fetchall()
                category_counts = {x['category']:x['n'] for x in category_rows}
                if category_counts.get('vendor_identity',0)<1 or category_counts.get('bank_account',0)<1:
                    raise HTTPException(409,'This onboarding request is missing identity or bank-account files. Reject it and submit a new request with complete documentary evidence.')
                if any(not x['integrity_valid'] for x in _approval_attachment_rows(conn,request_id)):
                    raise HTTPException(409,'One or more evidence files are missing or failed integrity validation. Do not approve this request.')
            if decision=='REJECT':
                conn.execute("UPDATE approval_requests SET status='REJECTED',rejected_by=?,rejected_at=?,updated_at=? WHERE id=?",(user['id'],now,now,request_id))
                conn.execute('INSERT INTO approval_records (id,request_id,user_id,decision,reason,created_at) VALUES (?,?,?,?,?,?)',(str(uuid.uuid4()),request_id,user['id'],'REJECT_FIRST',reason,now))
                event='VENDOR_ONBOARDING_REJECTED' if req_type=='VENDOR_ONBOARDING' else 'VENDOR_PROFILE_CHANGE_REJECTED'
                audit(conn,event,'REJECTED',actor=user,reason=reason or 'Request rejected at the first independent review.',details={'request_id':request_id,'requester_id':ar['requested_by']},approval_id=request_id)
                request.session['flash_success']='Request rejected. No vendor data or bank account was activated.'
            else:
                conn.execute('INSERT INTO approval_records (id,request_id,user_id,decision,reason,created_at) VALUES (?,?,?,?,?,?)',(str(uuid.uuid4()),request_id,user['id'],'APPROVE_FIRST',reason,now))
                conn.execute("UPDATE approval_requests SET status='PENDING_SECOND_APPROVER',first_approval_at=?,updated_at=? WHERE id=?",(now,now,request_id))
                event='VENDOR_ONBOARDING_FIRST_APPROVAL' if req_type=='VENDOR_ONBOARDING' else 'VENDOR_PROFILE_CHANGE_FIRST_APPROVAL'
                audit(conn,event,'PENDING_SECOND_APPROVER',actor=user,reason=reason or 'First independent review completed; a different second approver is required.',details={'request_id':request_id,'requester_id':ar['requested_by']},approval_id=request_id)
                request.session['flash_success']='First review recorded. A different supervisor or administrator must provide the final approval.'
            return redirect('/approvals')

        if ar['status']!='PENDING_SECOND_APPROVER':
            raise HTTPException(409,'This request is no longer awaiting approval.')
        if not require_roles(user,'supervisor','admin'):
            raise HTTPException(403,'Only a supervisor or administrator can provide the final approval.')
        if ar['requested_by']==user['id']:
            raise HTTPException(403,'The requester cannot act as an approver.')
        first_approver_id=None
        if req_type in multi_stage_types:
            first_record=conn.execute("SELECT user_id FROM approval_records WHERE request_id=? AND decision='APPROVE_FIRST' ORDER BY created_at LIMIT 1",(request_id,)).fetchone()
            if not first_record:
                raise HTTPException(409,'The first approval record is missing; this request cannot be finalized.')
            first_approver_id=first_record['user_id']
            if first_approver_id==user['id']:
                raise HTTPException(403,'The second approver must be different from the requester and first approver.')
            if not context_fingerprint_matches(context):
                raise HTTPException(409,'The submitted request payload changed after review. Submit a new request.')

        if req_type in {'VENDOR_ONBOARDING','VENDOR_PROFILE_CHANGE','BANK_ACCOUNT_CHANGE'}:
            if review_confirmed.lower() not in {'1','true','yes','on'}:
                raise HTTPException(400, 'Confirm that you reviewed the vendor details and supporting evidence before recording a decision.')
            reviewed_state = request.session.get('approval_reviewed_requests', {})
            expected_review_fingerprint = context.get('payload_sha256') or payload_fingerprint(context)
            if reviewed_state.get(request_id) != expected_review_fingerprint:
                raise HTTPException(409, 'Open Review Details and inspect the current request before recording a decision.')
        if req_type == 'VENDOR_ONBOARDING':
            request_attachments = conn.execute('SELECT category, COUNT(*) AS n FROM approval_attachments WHERE request_id=? GROUP BY category', (request_id,)).fetchall()
            category_counts = {row['category']: row['n'] for row in request_attachments}
            if category_counts.get('vendor_identity', 0) < 1 or category_counts.get('bank_account', 0) < 1:
                raise HTTPException(409, 'This onboarding request is missing identity or bank-account files. Reject it and submit a new request with complete documentary evidence.')
            stored_attachments = _approval_attachment_rows(conn, request_id)
            if not stored_attachments or any(not x['integrity_valid'] for x in stored_attachments):
                raise HTTPException(409, 'One or more evidence files are missing or failed integrity validation. Do not approve this request.')

        evidence=ar['evidence'] or ''
        if decision=='REJECT':
            conn.execute("UPDATE approval_requests SET status='REJECTED',rejected_by=?,rejected_at=?,updated_at=? WHERE id=?",(user['id'],now,now,request_id))
            conn.execute('INSERT INTO approval_records (id,request_id,user_id,decision,reason,created_at) VALUES (?,?,?,?,?,?)',(str(uuid.uuid4()),request_id,user['id'],'REJECT',reason,now))
            if req_type=='RED_RESOLUTION':
                audit_invoice_event(conn,ar['entity_id'],user,'RED_RESOLUTION_REJECTED','REJECTED',reason or 'Resolution request rejected.',approval_id=request_id)
            elif req_type=='BANK_ACCOUNT_CHANGE':
                audit(conn,'VENDOR_BANK_CHANGE_REJECTED','REJECTED',actor=user,reason=reason or 'Bank-account change rejected.',details={'vendor_id':ar['entity_id']},approval_id=request_id)
            else:
                event='VENDOR_ONBOARDING_REJECTED' if req_type=='VENDOR_ONBOARDING' else 'VENDOR_PROFILE_CHANGE_REJECTED'
                audit(conn,event,'REJECTED',actor=user,reason=reason or 'Request rejected at final review.',details={'request_id':request_id,'requester_id':ar['requested_by'],'first_approver_id':first_approver_id},approval_id=request_id)
            request.session['flash_success']='Request rejected. The prior vendor and bank-account state has been preserved.'
            return redirect('/approvals')

        if len(evidence.strip())<10:
            raise HTTPException(400,'The requester’s verification evidence is insufficient.')
        if req_type in multi_stage_types:
            if req_type=='VENDOR_ONBOARDING':
                vendor_data=context.get('vendor') or {}
                bank_data=context.get('bank') or {}
                verification=context.get('verification') or {}
                if len(str(verification.get('identity_evidence','')).strip())<10 or len(str(verification.get('bank_evidence','')).strip())<10:
                    raise HTTPException(400,'Both vendor identity evidence and bank-account verification evidence are required.')
                if conn.execute('SELECT 1 FROM vendors WHERE vendor_code=?',(vendor_data.get('vendor_code'),)).fetchone():
                    raise HTTPException(409,'The vendor code is now in use. Submit a new request with a unique code.')
                duplicate_flags=vendor_duplicate_flags(conn,company_name=vendor_data.get('company_name',''),tax_id=vendor_data.get('tax_id'),account_number=bank_data.get('account_number'))
                newly_detected_flags=sorted(set(duplicate_flags)-set(context.get('duplicate_flags',[])))
                if newly_detected_flags:
                    raise HTTPException(409,'New duplicate warning(s) appeared after the reviewers began the process. Reject this request and submit a new one so both reviewers can inspect the updated warnings.')
                if duplicate_flags and len(reason.strip())<10:
                    raise HTTPException(400,'Potential duplicates were detected. Record a review note of at least 10 characters before final approval.')
                vid='vendor-'+uuid.uuid4().hex[:12]
                conn.execute("""INSERT INTO vendors
                    (id,vendor_code,company_name,address,tax_id,verification_status,current_bank_name,current_bank_account_number,current_bank_account_holder,country,contact_person,email,phone,created_at,updated_at)
                    VALUES (?,?,?,?,?,'VERIFIED',?,?,?,?,?,?,?,?,?)""",(
                    vid,vendor_data['vendor_code'],vendor_data['company_name'],vendor_data.get('address'),vendor_data.get('tax_id'),
                    bank_data['bank_name'],bank_data['account_number'],bank_data['account_holder_name'],vendor_data.get('country'),
                    vendor_data.get('contact_person'),vendor_data.get('email'),vendor_data.get('phone'),now,now))
                conn.execute("""INSERT INTO vendor_bank_accounts
                    (id,vendor_id,bank_name,account_number,account_holder_name,status,change_request_id,created_at,verified_at,verified_by,bank_country,swift_bic)
                    VALUES (?,?,?,?,?,'VERIFIED',?,?,?,?,?,?)""",(
                    str(uuid.uuid4()),vid,bank_data['bank_name'],bank_data['account_number'],bank_data['account_holder_name'],request_id,now,now,user['id'],bank_data.get('bank_country'),bank_data.get('swift_bic')))
                # Reconcile old POs only when strong identity evidence (exact tax ID) supports it.
                relink_result = relink_unlinked_purchase_orders(conn, vid, actor=user)
                audit(conn,'VENDOR_ONBOARDING_APPROVED','APPROVED',actor=user,reason=reason or 'Vendor and payment destination approved after two independent reviews.',details={
                    'request_id':request_id,'vendor_id':vid,'vendor_code':vendor_data['vendor_code'],'company_name':vendor_data['company_name'],
                    'bank_name':bank_data['bank_name'],'account_masked':mask_account(bank_data['account_number']),
                    'requester_id':ar['requested_by'],'first_approver_id':first_approver_id,'second_approver_id':user['id'],
                    'duplicate_flags':duplicate_flags,'payload_sha256':context.get('payload_sha256'),
                    'po_relink_summary': {k: len(v) for k, v in relink_result.items()},
                },approval_id=request_id)
            else:  # VENDOR_PROFILE_CHANGE
                vendor=conn.execute('SELECT * FROM vendors WHERE id=?',(ar['entity_id'],)).fetchone()
                if not vendor: raise HTTPException(404,'Vendor not found.')
                if vendor['updated_at'] != context.get('original_updated_at'):
                    raise HTTPException(409,'The vendor profile changed while this request was awaiting approval. Submit a fresh change request.')
                before=context.get('before') or {}; after=context.get('after') or {}
                duplicates=vendor_duplicate_flags(conn,company_name=after.get('company_name') or vendor['company_name'],tax_id=after.get('tax_id'),exclude_vendor_id=vendor['id'])
                newly_detected_flags=sorted(set(duplicates)-set(context.get('duplicate_flags',[])))
                if newly_detected_flags:
                    raise HTTPException(409,'New duplicate warning(s) appeared after the reviewers began the process. Reject this request and submit a new one so both reviewers can inspect the updated warnings.')
                if duplicates and len(reason.strip())<10:
                    raise HTTPException(400,'Potential duplicates were detected. Record a review note of at least 10 characters before final approval.')
                permitted=['company_name','address','tax_id','country','contact_person','email','phone']
                fields=[f for f in permitted if f in after and after.get(f) != before.get(f)]
                if not fields: raise HTTPException(400,'No supported profile changes were submitted.')
                assignments=', '.join(f'{field}=?' for field in fields)
                values=[after[f] for f in fields]
                conn.execute(f'UPDATE vendors SET {assignments}, updated_at=? WHERE id=?',(*values,now,vendor['id']))
                audit(conn,'VENDOR_PROFILE_UPDATED','APPROVED',actor=user,reason=reason or 'Vendor profile change was approved by two different reviewers.',details={
                    'request_id':request_id,'vendor_id':vendor['id'],'changed_fields':fields,
                    'before':{f:before.get(f) for f in fields},'after':{f:after.get(f) for f in fields},
                    'requester_id':ar['requested_by'],'first_approver_id':first_approver_id,'second_approver_id':user['id'],'duplicate_flags':duplicates,
                },approval_id=request_id)
                relink_unlinked_purchase_orders(conn, vendor['id'], actor=user)
            conn.execute("UPDATE approval_requests SET status='APPROVED',approved_by=?,approved_at=?,updated_at=? WHERE id=?",(user['id'],now,now,request_id))
            conn.execute('INSERT INTO approval_records (id,request_id,user_id,decision,reason,created_at) VALUES (?,?,?,?,?,?)',(str(uuid.uuid4()),request_id,user['id'],'APPROVE',reason,now))
            request.session['flash_success']='Final approval recorded. The approved vendor data has been activated and the audit trail retained.'
            return redirect('/approvals')

        if len(evidence.strip())<10:
            raise HTTPException(400,'The requester’s verification evidence is insufficient.')
        conn.execute("UPDATE approval_requests SET status='APPROVED',approved_by=?,approved_at=?,updated_at=? WHERE id=?",(user['id'],now,now,request_id))
        conn.execute('INSERT INTO approval_records (id,request_id,user_id,decision,reason,created_at) VALUES (?,?,?,?,?,?)',(str(uuid.uuid4()),request_id,user['id'],'APPROVE',reason,now))
        if req_type=='BANK_ACCOUNT_CHANGE':
            vendor=conn.execute('SELECT * FROM vendors WHERE id=?',(ar['entity_id'],)).fetchone()
            if not vendor: raise HTTPException(404,'Vendor not found.')
            if vendor['current_bank_account_number'] != context.get('old_account_number'):
                raise HTTPException(409,'The vendor bank account has changed since the request was created. Create a new request using the latest data.')
            conn.execute("UPDATE vendor_bank_accounts SET status='SUPERSEDED' WHERE vendor_id=? AND status='VERIFIED'",(vendor['id'],))
            account=context['account_number']
            conn.execute("""INSERT INTO vendor_bank_accounts (id,vendor_id,bank_name,account_number,account_holder_name,status,change_request_id,created_at,verified_at,verified_by,bank_country,swift_bic)
                VALUES (?,?,?,?,?,'VERIFIED',?,?,?,?,?,?)""",(str(uuid.uuid4()),vendor['id'],context['bank_name'],account,context['account_holder'],request_id,now,now,user['id'],context.get('bank_country'),context.get('swift_bic')))
            conn.execute("UPDATE vendors SET current_bank_name=?,current_bank_account_number=?,current_bank_account_holder=?,verification_status='VERIFIED',updated_at=? WHERE id=?",(context['bank_name'],account,context['account_holder'],now,vendor['id']))
            audit(conn,'VENDOR_BANK_CHANGE_APPROVED','APPROVED',actor=user,invoice_id=context.get('invoice_id'),reason=reason or 'Bank-account change approved by a second person after verification.',details={'vendor_id':vendor['id'],'new_account_masked':mask_account(account),'requester_id':ar['requested_by']},approval_id=request_id)
            if context.get('invoice_id'):
                iid=context['invoice_id']; evaluate_invoice(conn,iid)
                audit_invoice_event(conn,iid,user,'RISK_REEVALUATED_AFTER_BANK_CHANGE','COMPLETED','Risk analysis was rerun after the vendor bank account was verified.',{'bank_change_request':request_id})
        elif req_type=='RED_RESOLUTION':
            iid=ar['entity_id']
            inv=get_invoice(conn,iid)
            po=conn.execute('SELECT total, currency FROM purchase_orders WHERE po_number=?',(inv['purchase_order_number'],)).fetchone() if inv else None
            if not inv or not po:
                raise HTTPException(409,'The PO or invoice is no longer available; resolution was cancelled and the case remains blocked.')
            if (inv['currency'] or DEFAULT_CURRENCY).upper() != (po['currency'] or DEFAULT_CURRENCY).upper():
                raise HTTPException(409,'Invoice and PO currencies differ; a currency mismatch cannot be approved as a PO amount exception.')
            try:
                same_invoice_total=abs(float(context.get('invoice_total'))-float(inv['total'])) < 0.01
                same_po_total=abs(float(context.get('po_total'))-float(po['total'])) < 0.01
                same_po_number=context.get('po_number') == inv['purchase_order_number']
                same_currencies=context.get('invoice_currency') == (inv['currency'] or DEFAULT_CURRENCY).upper() and context.get('po_currency') == (po['currency'] or DEFAULT_CURRENCY).upper()
            except Exception:
                same_invoice_total=same_po_total=same_po_number=same_currencies=False
            if not (same_invoice_total and same_po_total and same_po_number and same_currencies):
                raise HTTPException(409,'The invoice/PO amount or currency changed after the request was created. The old request cannot be approved; submit a new request after re-analysis.')
            ctx={**context,'resolved_finding_codes':['PO_MISMATCH']}
            conn.execute('UPDATE approval_requests SET context_json=? WHERE id=?',(json.dumps(ctx,ensure_ascii=False),request_id))
            audit_invoice_event(conn,iid,user,'RED_RESOLUTION_DUAL_APPROVED','APPROVED','The PO mismatch exception was reviewed by two people and is limited to the approved amounts; normal payment approval is still required.',{'requester_id':ar['requested_by'],'evidence':evidence,'invoice_total':ctx['invoice_total'],'po_total':ctx['po_total']},approval_id=request_id)
            result=evaluate_invoice(conn,iid)
            audit_invoice_event(conn,iid,user,'RISK_REEVALUATED_AFTER_RED_REVIEW','COMPLETED','The risk engine continues to evaluate active facts after resolution.',{'current_risk':result['risk_level']},approval_id=request_id)
        request.session['flash_success']='Second approval recorded. The system updated the permitted state and preserved the audit trail.'
    return redirect('/approvals')

@app.get('/approvals/{request_id}/evidence/{attachment_id}')
def approval_evidence_download(request: Request, request_id: str, attachment_id: str):
    user=user_or_redirect(request)
    if not isinstance(user,dict): return user
    if not require_roles(user,'finance','supervisor','admin'):
        raise HTTPException(403,'You are not authorized to view vendor evidence.')
    with get_db() as conn:
        row=conn.execute('SELECT * FROM approval_attachments WHERE id=? AND request_id=?',(attachment_id,request_id)).fetchone()
        if not row: raise HTTPException(404,'Evidence attachment not found.')
        request_row=conn.execute('SELECT context_json FROM approval_requests WHERE id=?',(request_id,)).fetchone()
        context=safe_json(request_row['context_json'],{}) if request_row else {}
        expected={x.get('id'):x for x in (context.get('evidence_attachments') or []) if isinstance(x,dict)}.get(attachment_id) or {}
        if (expected.get('sha256') != row['sha256'] or expected.get('category') != row['category']
            or expected.get('filename') != row['original_filename'] or expected.get('byte_size') != row['byte_size']
            or expected.get('mime_type') != row['mime_type']):
            raise HTTPException(409,'Evidence metadata integrity verification failed.')
    base=(UPLOAD_DIR/'vendor-evidence'/request_id).resolve()
    path=(base/row['stored_filename']).resolve()
    try:
        path.relative_to(base)
        data=path.read_bytes()
    except (OSError, ValueError):
        raise HTTPException(404,'Evidence file is missing or unavailable.')
    if len(data)!=row['byte_size'] or not hmac.compare_digest(hashlib.sha256(data).hexdigest(),row['sha256']):
        raise HTTPException(409,'Evidence integrity verification failed. Do not use this file for approval.')
    inline = row['mime_type'] in {'application/pdf','image/png','image/jpeg','image/webp'}
    safe_name=re.sub(r'[^A-Za-z0-9._ -]', '_', _safe_evidence_filename(row['original_filename'])).replace('"','_')
    disposition='inline' if inline else 'attachment'
    return FileResponse(str(path), media_type=row['mime_type'], headers={
        'Content-Disposition': f'{disposition}; filename="{safe_name}"',
        'X-Content-Type-Options': 'nosniff', 'Cache-Control': 'private, no-store',
        'Content-Security-Policy': "default-src 'none'; sandbox",
    })


@app.get('/approvals/{request_id}',response_class=HTMLResponse)
def approval_detail_page(request: Request, request_id: str):
    user=user_or_redirect(request)
    if not isinstance(user,dict): return user
    if not require_roles(user,'finance','supervisor','admin'):
        raise HTTPException(403,'You are not authorized to review requests.')
    with get_db() as conn:
        row=conn.execute('''SELECT a.*,u.name requester_name,
            (SELECT ar.user_id FROM approval_records ar WHERE ar.request_id=a.id AND ar.decision='APPROVE_FIRST' ORDER BY ar.created_at LIMIT 1) first_approver_id,
            (SELECT u2.name FROM approval_records ar JOIN users u2 ON u2.id=ar.user_id WHERE ar.request_id=a.id AND ar.decision='APPROVE_FIRST' ORDER BY ar.created_at LIMIT 1) first_approver_name
            FROM approval_requests a LEFT JOIN users u ON u.id=a.requested_by WHERE a.id=?''',(request_id,)).fetchone()
        if not row: raise HTTPException(404,'Approval request not found.')
        item=dict(row); context=safe_json(item.get('context_json'),{})
        attachments=_approval_attachment_rows(conn,request_id)
        vendor=None
        if item['request_type']=='VENDOR_ONBOARDING':
            vendor=context.get('vendor') or {}; bank=context.get('bank') or {}; verification=context.get('verification') or {}
        elif item['request_type']=='VENDOR_PROFILE_CHANGE':
            vendor=conn.execute('SELECT * FROM vendors WHERE id=?',(item['entity_id'],)).fetchone()
            bank={}; verification={'identity_evidence':item.get('evidence') or '', 'bank_evidence':''}
        elif item['request_type']=='BANK_ACCOUNT_CHANGE':
            vendor=conn.execute('SELECT * FROM vendors WHERE id=?',(item['entity_id'],)).fetchone()
            bank=context; verification={'identity_evidence':item.get('evidence') or '', 'bank_evidence':item.get('evidence') or ''}
        else:
            vendor=context.get('vendor') or {}; bank={}; verification=context.get('verification') or {}
        if item['request_type']=='VENDOR_PROFILE_CHANGE':
            profile_before=context.get('before') or {}; profile_after=context.get('after') or {}
        else:
            profile_before={}; profile_after={}
        duplicate_flags=context.get('duplicate_flags') or []
        if item['request_type']=='VENDOR_ONBOARDING':
            attachment_categories={x['category'] for x in attachments if x.get('integrity_valid')}
            evidence_ready=('vendor_identity' in attachment_categories and 'bank_account' in attachment_categories
                            and bool(attachments) and all(x.get('integrity_valid') for x in attachments))
        else:
            evidence_ready=True
    reviewed_state = request.session.get('approval_reviewed_requests', {})
    reviewed_state[request_id] = context.get('payload_sha256') or payload_fingerprint(context)
    # Keep the signed session cookie small if a reviewer opens many requests.
    if len(reviewed_state) > 20:
        reviewed_state = dict(list(reviewed_state.items())[-20:])
    request.session['approval_reviewed_requests'] = reviewed_state
    item['context']=context
    item['attachments']=attachments
    item['vendor_detail']=dict(vendor) if vendor and not isinstance(vendor,dict) else vendor
    item['bank_detail']=bank
    item['verification_detail']=verification
    item['profile_before']=profile_before
    item['profile_after']=profile_after
    item['duplicate_flags_named']=[x.replace('_',' ').title() for x in duplicate_flags]
    is_vendor_request=item['request_type'] in {'VENDOR_ONBOARDING','VENDOR_PROFILE_CHANGE','BANK_ACCOUNT_CHANGE'}
    can_first=(item['status']=='PENDING_FIRST_APPROVER' and item['requested_by']!=user['id'] and require_roles(user,'finance','supervisor','admin'))
    can_final=(item['status']=='PENDING_SECOND_APPROVER' and item['requested_by']!=user['id'] and require_roles(user,'supervisor','admin') and item.get('first_approver_id')!=user['id'])
    return render(request,'approval_detail.html',{'approval':item,'context':context,'vendor_detail':item['vendor_detail'],
        'bank_detail':bank,'verification_detail':verification,'attachments':attachments,'can_first_review':can_first,
        'can_final_review':can_final,'is_vendor_request':is_vendor_request,'evidence_ready':evidence_ready,'flash_error':request.session.pop('flash_error',None),
        'flash_success':request.session.pop('flash_success',None)})


@app.get('/approvals',response_class=HTMLResponse)
def approvals_page(request: Request):
    user=user_or_redirect(request)
    if not isinstance(user,dict): return user
    with get_db() as conn:
        requests=conn.execute('''SELECT a.*,u.name requester_name,
            (SELECT ar.user_id FROM approval_records ar WHERE ar.request_id=a.id AND ar.decision='APPROVE_FIRST' ORDER BY ar.created_at LIMIT 1) first_approver_id,
            (SELECT u2.name FROM approval_records ar JOIN users u2 ON u2.id=ar.user_id WHERE ar.request_id=a.id AND ar.decision='APPROVE_FIRST' ORDER BY ar.created_at LIMIT 1) first_approver_name,
            (SELECT COUNT(*) FROM approval_attachments aa WHERE aa.request_id=a.id) attachment_count
            FROM approval_requests a LEFT JOIN users u ON u.id=a.requested_by
            ORDER BY CASE a.status WHEN 'PENDING_FIRST_APPROVER' THEN 0 WHEN 'PENDING_SECOND_APPROVER' THEN 1 ELSE 2 END, a.created_at DESC LIMIT 100''').fetchall()
    enriched=[]
    for row in requests:
        item=dict(row); context=safe_json(item.get('context_json'),{})
        item['review_evidence']=item.get('evidence') or '—'
        if item['request_type']=='BANK_ACCOUNT_CHANGE':
            item['context_summary']=f"New bank: {context.get('bank_name','—')} · New account: {mask_account(context.get('account_number'))} · Account holder: {context.get('account_holder','—')}"
        elif item['request_type']=='VENDOR_ONBOARDING':
            v=context.get('vendor') or {}; b=context.get('bank') or {}
            flags=', '.join(x.replace('_',' ').title() for x in context.get('duplicate_flags',[])) or 'No duplicate flags at submission'
            item['context_summary']=(f"Vendor: {v.get('company_name','—')} ({v.get('vendor_code','—')}) · Address: {v.get('address') or '—'} · "
                f"Tax ID: {v.get('tax_id') or '—'} · Country: {v.get('country') or '—'} · Contact: {v.get('contact_person') or '—'} · "
                f"Email: {v.get('email') or '—'} · Phone: {v.get('phone') or '—'} · Bank: {b.get('bank_name','—')} · "
                f"Bank country: {b.get('bank_country') or '—'} · Account: {mask_account(b.get('account_number'))} · "
                f"Account holder: {b.get('account_holder_name') or '—'} · SWIFT/BIC: {b.get('swift_bic') or '—'} · Review flags: {flags}")
            verification=context.get('verification') or {}
            item['review_evidence']=f"Identity evidence: {verification.get('identity_evidence','—')} · Bank evidence: {verification.get('bank_evidence','—')}"
        elif item['request_type']=='VENDOR_PROFILE_CHANGE':
            after=context.get('after') or {}; before=context.get('before') or {}
            changed=', '.join(f"{k.replace('_',' ').title()}: {before.get(k) or '—'} → {after.get(k) or '—'}" for k in after if before.get(k)!=after.get(k)) or 'Profile change'
            flags=', '.join(x.replace('_',' ').title() for x in context.get('duplicate_flags',[])) or 'No duplicate flags at submission'
            item['context_summary']=f"Vendor: {context.get('vendor_name','—')} · Proposed fields: {changed} · Review flags: {flags}"
            item['review_evidence']=item.get('evidence') or '—'
        elif item['request_type']=='RED_RESOLUTION':
            item['context_summary']=f"Invoice: {context.get('invoice_id','—')} · PO: {context.get('po_number','—')} · Invoice total: {context.get('invoice_total','—')} · PO total: {context.get('po_total','—')}"
        else:
            item['context_summary']='—'
        enriched.append(item)
    return render(request,'approvals.html',{'requests':enriched,'flash_error':request.session.pop('flash_error',None),'flash_success':request.session.pop('flash_success',None)})

@app.get('/vendors',response_class=HTMLResponse)
def vendors_page(request: Request):
    user=user_or_redirect(request)
    if not isinstance(user,dict): return user
    with get_db() as conn:
        vendors=conn.execute('''SELECT v.*, (SELECT COUNT(*) FROM invoices i WHERE i.vendor_id=v.id) invoice_count,
            (SELECT COUNT(*) FROM vendor_bank_accounts b WHERE b.vendor_id=v.id AND b.status='VERIFIED') verified_accounts
            FROM vendors v ORDER BY company_name''').fetchall()
        pending_onboarding=conn.execute("SELECT COUNT(*) FROM approval_requests WHERE request_type='VENDOR_ONBOARDING' AND status IN ('PENDING_FIRST_APPROVER','PENDING_SECOND_APPROVER')").fetchone()[0]
    return render(request,'vendors.html',{'vendors':vendors,'pending_onboarding':pending_onboarding,'can_manage_vendors':require_roles(user,'finance','supervisor','admin'),'flash_error':request.session.pop('flash_error',None),'flash_success':request.session.pop('flash_success',None)})

@app.post('/vendors')
def add_vendor(request: Request, csrf_token: str = Form(...), vendor_code: str = Form(...), company_name: str = Form(...),
               address: str = Form(''), tax_id: str = Form(''), country: str = Form(''), contact_person: str = Form(''),
               email: str = Form(''), phone: str = Form(''), bank_name: str = Form(...), bank_country: str = Form(''),
               account_holder_name: str = Form(...), account_number: str = Form(...), swift_bic: str = Form(''),
               identity_evidence: str = Form(...), bank_evidence: str = Form(...), reason: str = Form(...),
               identity_files: list[UploadFile] = File(default=[]), bank_files: list[UploadFile] = File(default=[])):
    user=user_or_redirect(request)
    if not isinstance(user,dict): return user
    check_csrf_or_400(request,csrf_token)
    if not require_roles(user,'finance','supervisor','admin'): raise HTTPException(403,'You are not authorized to submit vendor onboarding requests.')
    code=vendor_code.strip().upper(); name=company_name.strip()
    if not code or not name: raise HTTPException(400,'Vendor code and name are required.')
    account=re.sub(r'[\s-]','',account_number).upper()
    if not re.fullmatch(r'[A-Z0-9]{5,34}',account): raise HTTPException(400,'Enter a valid account identifier using 5–34 letters or numbers. Do not include special characters other than spaces or hyphens.')
    if not bank_name.strip() or not account_holder_name.strip(): raise HTTPException(400,'Bank name and account holder are required.')
    if len(identity_evidence.strip())<10 or len(bank_evidence.strip())<10: raise HTTPException(400,'Independent vendor identity evidence and bank-account verification evidence are required.')
    if len(reason.strip())<5: raise HTTPException(400,'Explain why this vendor is being onboarded.')
    identity_uploads = prepare_evidence_uploads(identity_files, 'vendor_identity')
    bank_uploads = prepare_evidence_uploads(bank_files, 'bank_account')
    if not identity_uploads:
        raise HTTPException(400, 'Upload at least one vendor identity document (PDF, image, or supported document format).')
    if not bank_uploads:
        raise HTTPException(400, 'Upload at least one bank-account ownership evidence document.')
    uploads = identity_uploads + bank_uploads
    if sum(item['byte_size'] for item in uploads) > min(MAX_UPLOAD_BYTES * 4, 40 * 1024 * 1024):
        raise HTTPException(413, 'The combined evidence upload exceeds the request size limit.')
    vendor_data={
        'vendor_code':code,'company_name':name,'address':address.strip() or None,'tax_id':tax_id.strip() or None,
        'country':country.strip() or None,'contact_person':contact_person.strip() or None,
        'email':email.strip() or None,'phone':phone.strip() or None,
    }
    bank_data={'bank_name':bank_name.strip(),'bank_country':bank_country.strip() or None,
               'account_holder_name':account_holder_name.strip(),'account_number':account,
               'swift_bic':swift_bic.strip().upper() or None}
    rid=str(uuid.uuid4()); now=utcnow(); saved_dir = UPLOAD_DIR / 'vendor-evidence' / rid
    try:
        with get_db() as conn:
            if conn.execute('SELECT 1 FROM vendors WHERE vendor_code=?',(code,)).fetchone():
                raise HTTPException(409,'This vendor code is already in use.')
            flags=vendor_duplicate_flags(conn,company_name=name,tax_id=vendor_data['tax_id'],account_number=account)
            evidence_meta = [{'id':x['id'],'category':x['category'],'filename':x['original_filename'],'sha256':x['sha256'],
                              'byte_size':x['byte_size'],'mime_type':x['mime_type']} for x in uploads]
            context={'vendor':vendor_data,'bank':bank_data,
                     'verification':{'identity_evidence':identity_evidence.strip(),'bank_evidence':bank_evidence.strip()},
                     'duplicate_flags':flags,'evidence_attachments':evidence_meta}
            context['payload_sha256']=payload_fingerprint(context)
            conn.execute('''INSERT INTO approval_requests (id,request_type,entity_type,entity_id,status,requested_by,reason,evidence,context_json,created_at,updated_at)
                VALUES (?,'VENDOR_ONBOARDING','vendor_onboarding',?,'PENDING_FIRST_APPROVER',?,?,?,?,?,?)''',
                (rid,rid,user['id'],reason.strip(),'Vendor identity evidence and bank-account evidence are attached to the protected request.',json.dumps(context,ensure_ascii=False),now,now))
            # Persist private files under a generated request ID; never use client names as paths.
            persist_evidence_uploads(rid, user['id'], uploads, conn)
            audit(conn,'VENDOR_ONBOARDING_SUBMITTED','PENDING_FIRST_APPROVER',actor=user,reason=reason.strip(),details={
                'request_id':rid,'vendor_code':code,'company_name':name,'bank_name':bank_data['bank_name'],
                'account_masked':mask_account(account),'duplicate_flags':flags,
                'evidence_attachment_count':len(uploads),'evidence_categories':['vendor_identity','bank_account'],
            },approval_id=rid)
    except Exception:
        if saved_dir.exists():
            shutil.rmtree(saved_dir, ignore_errors=True)
        raise
    request.session['flash_success']='Vendor onboarding request submitted with documentary evidence. The vendor and bank account remain inactive until two different reviewers approve it.'
    return redirect('/vendors')

@app.post('/vendors/{vendor_id}/profile-change/request')
def request_vendor_profile_change(request: Request, vendor_id: str, csrf_token: str = Form(...), company_name: str = Form(...),
                                  address: str = Form(''), tax_id: str = Form(''), country: str = Form(''),
                                  contact_person: str = Form(''), email: str = Form(''), phone: str = Form(''), reason: str = Form(...), evidence: str = Form(...)):
    user=user_or_redirect(request)
    if not isinstance(user,dict): return user
    check_csrf_or_400(request,csrf_token)
    if not require_roles(user,'finance','supervisor','admin'): raise HTTPException(403,'You are not authorized to request vendor profile changes.')
    after={'company_name':company_name.strip(),'address':address.strip() or None,'tax_id':tax_id.strip() or None,
           'country':country.strip() or None,'contact_person':contact_person.strip() or None,
           'email':email.strip() or None,'phone':phone.strip() or None}
    if not after['company_name']: raise HTTPException(400,'Company name is required.')
    if len(reason.strip())<5 or len(evidence.strip())<10: raise HTTPException(400,'A reason and independent evidence are required for profile changes.')
    with get_db() as conn:
        vendor=conn.execute('SELECT * FROM vendors WHERE id=?',(vendor_id,)).fetchone()
        if not vendor: raise HTTPException(404,'Vendor not found.')
        pending=conn.execute("SELECT 1 FROM approval_requests WHERE request_type='VENDOR_PROFILE_CHANGE' AND entity_id=? AND status IN ('PENDING_FIRST_APPROVER','PENDING_SECOND_APPROVER') LIMIT 1",(vendor_id,)).fetchone()
        if pending: raise HTTPException(409,'This vendor already has a profile change awaiting approval.')
        before={k:vendor[k] for k in after.keys()}
        if before==after: raise HTTPException(400,'No vendor profile changes were detected.')
        duplicates=vendor_duplicate_flags(conn,company_name=after['company_name'],tax_id=after['tax_id'],exclude_vendor_id=vendor_id)
        context={'vendor_id':vendor_id,'vendor_name':vendor['company_name'],'original_updated_at':vendor['updated_at'],
                 'before':before,'after':after,'duplicate_flags':duplicates}
        context['payload_sha256']=payload_fingerprint(context)
        rid=str(uuid.uuid4()); now=utcnow()
        conn.execute('''INSERT INTO approval_requests (id,request_type,entity_type,entity_id,status,requested_by,reason,evidence,context_json,created_at,updated_at)
            VALUES (?,'VENDOR_PROFILE_CHANGE','vendor',?,'PENDING_FIRST_APPROVER',?,?,?,?,?,?)''',
            (rid,vendor_id,user['id'],reason.strip(),evidence.strip(),json.dumps(context,ensure_ascii=False),now,now))
        audit(conn,'VENDOR_PROFILE_CHANGE_SUBMITTED','PENDING_FIRST_APPROVER',actor=user,reason=reason.strip(),details={
            'request_id':rid,'vendor_id':vendor_id,'changed_fields':[k for k in after if after[k]!=before.get(k)],'duplicate_flags':duplicates,
        },approval_id=rid)
    request.session['flash_success']='Vendor profile change submitted. The current profile remains unchanged until two different reviewers approve it.'
    return redirect(f'/vendors/{vendor_id}')

@app.get('/vendors/{vendor_id}',response_class=HTMLResponse)
def vendor_detail(request: Request,vendor_id:str):
    user=user_or_redirect(request)
    if not isinstance(user,dict): return user
    with get_db() as conn:
        vendor=conn.execute('SELECT * FROM vendors WHERE id=?',(vendor_id,)).fetchone()
        if not vendor: raise HTTPException(404,'Vendor not found.')
        accounts=conn.execute('SELECT * FROM vendor_bank_accounts WHERE vendor_id=? ORDER BY created_at DESC',(vendor_id,)).fetchall()
        pending=conn.execute("SELECT * FROM approval_requests WHERE entity_type='vendor' AND entity_id=? AND request_type IN ('BANK_ACCOUNT_CHANGE','VENDOR_PROFILE_CHANGE') ORDER BY created_at DESC LIMIT 10",(vendor_id,)).fetchall()
    return render(request,'vendor_detail.html',{'vendor':vendor,'accounts':accounts,'pending':pending,
        'can_manage_vendors':require_roles(user,'finance','supervisor','admin'),
        'flash_error':request.session.pop('flash_error',None),'flash_success':request.session.pop('flash_success',None)})

@app.get('/purchase-orders',response_class=HTMLResponse)
def purchase_orders_page(request: Request):
    user=user_or_redirect(request)
    if not isinstance(user,dict): return user
    with get_db() as conn:
        pos=conn.execute('''SELECT p.*,v.company_name AS trusted_vendor_name FROM purchase_orders p LEFT JOIN vendors v ON v.id=p.vendor_id ORDER BY p.created_at DESC LIMIT 250''').fetchall()
    return render(request,'purchase_orders.html',{'purchase_orders':pos,'flash_error':request.session.pop('flash_error',None),'flash_success':request.session.pop('flash_success',None)})

@app.get('/purchase-orders/{po_id}',response_class=HTMLResponse)
def purchase_order_detail_page(request: Request, po_id: str):
    user=user_or_redirect(request)
    if not isinstance(user,dict): return user
    with get_db() as conn:
        po=conn.execute('''SELECT p.*,v.company_name AS trusted_vendor_name,v.verification_status AS trusted_vendor_status,
            v.tax_id AS trusted_vendor_tax_id FROM purchase_orders p LEFT JOIN vendors v ON v.id=p.vendor_id WHERE p.id=?''',(po_id,)).fetchone()
        if not po:
            raise HTTPException(404,'Purchase order not found.')
        items=conn.execute('SELECT * FROM purchase_order_items WHERE po_id=? ORDER BY COALESCE(line_number, 999999), rowid',(po_id,)).fetchall()
        try:
            extracted=json.loads(po['extracted_json'] or '{}')
        except (TypeError,ValueError):
            extracted={}
        events=conn.execute('''SELECT a.*,u.name actor_name FROM audit_events a LEFT JOIN users u ON u.id=a.actor_id
            WHERE instr(a.details_json, ?) > 0 ORDER BY a.created_at DESC LIMIT 30''',(po['po_number'],)).fetchall()
        vendor_candidates = get_po_vendor_candidates(conn, po_id) if not po['vendor_id'] else []
    return render(request,'purchase_order_detail.html',{'po':po,'items':items,'extracted':extracted,'events':events,
        'vendor_candidates':vendor_candidates,
        'can_link_po_vendor':require_roles(user,'supervisor','admin'),
        'document_available':bool(po['document_path'] and Path(po['document_path']).exists()),
        'flash_error':request.session.pop('flash_error',None),'flash_success':request.session.pop('flash_success',None)})

@app.post('/purchase-orders/{po_id}/link-vendor')
def link_purchase_order_vendor_route(request: Request, po_id: str, csrf_token: str = Form(...),
                                     vendor_id: str = Form(...), reason: str = Form(''),
                                     confirm_name_only: str = Form(''), override_tax_conflict: str = Form('')):
    user = user_or_redirect(request)
    if not isinstance(user, dict):
        return user
    check_csrf_or_400(request, csrf_token)
    if not require_roles(user, 'supervisor', 'admin'):
        raise HTTPException(403, 'Only a supervisor or administrator can establish a trusted PO vendor link.')
    if len(reason.strip()) < 10:
        raise HTTPException(400, 'Provide a review reason of at least 10 characters.')
    try:
        with get_db() as conn:
            result = link_po_to_vendor(
                conn, po_id=po_id, vendor_id=vendor_id, actor=user, reason=reason,
                confirm_name_only=confirm_name_only.lower() in {'1','true','yes','on'},
                override_tax_conflict=override_tax_conflict.lower() in {'1','true','yes','on'},
            )
    except ValueError as exc:
        request.session['flash_error'] = str(exc)
        return redirect(f'/purchase-orders/{po_id}')
    if result.get('status') == 'ALREADY_MATCHED':
        request.session['flash_success'] = 'This PO is already linked to the selected vendor. No duplicate relationship was created.'
    else:
        request.session['flash_success'] = (f"Trusted vendor link established using {result.get('method')}. "
            f"Risk assessment was rerun for {result.get('reassessed_invoices', 0)} related invoice(s). Review any remaining findings.")
    return redirect(f'/purchase-orders/{po_id}')


@app.post('/purchase-orders/{po_id}/refresh-vendor-match')
def refresh_purchase_order_vendor_match(request: Request, po_id: str, csrf_token: str = Form(...)):
    user = user_or_redirect(request)
    if not isinstance(user, dict):
        return user
    check_csrf_or_400(request, csrf_token)
    if not require_roles(user, 'supervisor', 'admin'):
        raise HTTPException(403, 'Only a supervisor or administrator can reconcile PO vendor identity.')
    with get_db() as conn:
        po = conn.execute('SELECT * FROM purchase_orders WHERE id=?', (po_id,)).fetchone()
        if not po:
            raise HTTPException(404, 'Purchase order not found.')
        if po['vendor_id']:
            request.session['flash_success'] = 'This PO already has a persisted vendor relationship.'
        else:
            result = update_unlinked_po_candidate_state(conn, po_id, actor=user)
            request.session['flash_success'] = f"Vendor resolution status: {result['status']}. {result['reason']}"
            audit(conn, 'PO_VENDOR_MATCH_REVIEWED', result['status'], actor=user,
                  reason=result['reason'], document_hash=po['document_hash'],
                  details={'po_id':po_id,'po_number':po['po_number'],'status':result['status'],
                           'method':result.get('method'),'candidate_vendor_id':result.get('vendor',{}).get('id') if result.get('vendor') else None})
    return redirect(f'/purchase-orders/{po_id}')


@app.post('/purchase-orders/upload')
async def upload_po(request: Request,csrf_token:str=Form(...),file:UploadFile=File(...)):
    user=user_or_redirect(request)
    if not isinstance(user,dict): return user
    check_csrf_or_400(request,csrf_token)
    if not require_roles(user,'finance','supervisor','admin'): raise HTTPException(403,'You are not authorized to upload purchase orders.')
    data=await file.read(MAX_UPLOAD_BYTES + 1)
    try:
        mime=check_upload_bytes(file.filename or 'po.pdf',file.content_type,data)
        path,digest=store_bytes(data,file.filename or 'po.pdf')
        text,fields,items,_=extract_document(Path(path),file.filename or 'PO',utcnow(),document_type='purchase_order')
        local_po_fields = dict(fields)
        fields, extraction_engine, extraction_trace = optional_llm_extract(text, fields, document_type='purchase_order')
        # Resolve currency from source text/policy, not from an unsupported model guess.
        detected_currency, currency_confidence = detect_currency(text, DEFAULT_CURRENCY)
        fields['currency'] = detected_currency
        fields['currency_confidence'] = currency_confidence
        fields['currency_source'] = ('EXPLICIT_DOCUMENT_EVIDENCE' if currency_confidence == 'HIGH'
                                     else ('CONFIGURED_USD_POLICY' if currency_confidence == 'POLICY' else 'CONFIGURED_DEFAULT_UNCONFIRMED'))
        fields['extraction_source'] = extraction_engine
        fields['extraction_trace'] = extraction_trace
        for money_key in ('subtotal','discount','taxable_amount','tax','shipping','other_charges','rounding','total'):
            value=fields.get(money_key)
            if value is not None:
                fields[money_key] = float(value) if isinstance(value,(int,float)) else parse_amount(str(value), fields.get('currency'))
        # Normalize structured item values returned by an optional provider before persistence.
        items = fields.get('items', items) if isinstance(fields.get('items'), list) else items
        normalized_po_items=[]
        from .services.extraction import parse_number as _parse_number
        for item in items:
            if not isinstance(item,dict) or not (item.get('item_name') or item.get('description')):
                continue
            norm=dict(item)
            norm['item_name']=norm.get('item_name') or norm.get('description')
            for key in ('quantity','tax_rate'):
                if norm.get(key) is not None and not isinstance(norm[key],(int,float)):
                    norm[key]=_parse_number(str(norm[key]))
            for key in ('unit_price','item_discount','item_tax','line_total'):
                if norm.get(key) is not None and not isinstance(norm[key],(int,float)):
                    norm[key]=parse_amount(str(norm[key]), fields.get('currency'))
            norm.setdefault('description', None); norm.setdefault('sku', None); norm.setdefault('unit', None)
            normalized_po_items.append(norm)
        items=normalized_po_items
        fields['items']=items
        llm_candidate_fields=set(extraction_trace.get('provided_field_names') or [])
        fields['field_provenance'] = {key: ('MISSING' if value in (None, '', []) else ('EXTERNAL_LLM_CANDIDATE' if key in llm_candidate_fields and local_po_fields.get(key) in (None, '', []) else 'DOCUMENT_TEXT')) for key,value in fields.items() if key not in {'extraction_trace','field_provenance','items'}}
        fields['field_provenance']['currency'] = ('CONFIGURED_USD_POLICY' if currency_confidence == 'POLICY' else ('EXPLICIT_DOCUMENT_EVIDENCE' if currency_confidence == 'HIGH' else 'CONFIGURED_DEFAULT_UNCONFIRMED'))
        if not fields.get('purchase_order_number'):
            Path(path).unlink(missing_ok=True)
            raise ValueError('The PO number could not be extracted. Use a document that clearly shows the PO number.')
        with get_db() as conn:
            vendor_resolution = classify_po_vendor_identity(
                conn, supplier_name=fields.get('vendor_company_name'), supplier_tax_id=fields.get('vendor_tax_id'))
            vendor = vendor_resolution.get('vendor') if vendor_resolution.get('status') == 'MATCHED' else None
            if conn.execute('SELECT 1 FROM purchase_orders WHERE po_number=?',(fields['purchase_order_number'],)).fetchone():
                raise HTTPException(409,'This PO number already exists. Check whether the document is a duplicate.')
            poid=str(uuid.uuid4()); now=utcnow()
            grand_total=fields.get('total')
            model_supplied_total = 'total' in (extraction_trace.get('provided_field_names') or []) and local_po_fields.get('total') is None and grand_total is not None
            if grand_total is None:
                total_status='MISSING'
                total_source='MISSING'
            elif model_supplied_total:
                # An LLM-only amount is a candidate requiring human verification, not trusted PO evidence.
                total_status='REVIEW_REQUIRED'
                total_source='EXTERNAL_LLM_CANDIDATE'
            else:
                total_status='EXTRACTED'
                total_source='DOCUMENT_TEXT'
            fields['total_status']=total_status
            fields['total_source']=total_source
            fields['extraction_status'] = 'PARTIAL' if (grand_total is None or not fields.get('vendor_company_name') or not items) else 'COMPLETED'
            fields['missing_required_fields'] = [label for key,label in [('purchase_order_number','PO number'),('vendor_company_name','Supplier name'),('total','Grand total')] if not fields.get(key)]
            fields['total_status']=total_status
            fields['total_source']=total_source
            # Never substitute the subtotal for an unreadable grand total. The legacy
            # NOT NULL numeric column receives 0 only as storage compatibility; UI/risk
            # use total_status and must display it as unavailable.
            stored_total=grand_total if grand_total is not None else 0
            conn.execute('''INSERT INTO purchase_orders
                (id,po_number,vendor_id,issue_date,total,currency,source_filename,source_text,document_path,document_hash,mime_type,status,created_at,
                 vendor_company_name,vendor_address,vendor_tax_id,buyer_company_name,buyer_address,buyer_tax_id,subtotal,discount,tax,shipping,other_charges,rounding,
                 extracted_json,currency_confidence,total_status,total_source)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,'ACTIVE',?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (poid,fields['purchase_order_number'],vendor['id'] if vendor else None,fields.get('invoice_issue_date'),stored_total,
                 fields.get('currency') or DEFAULT_CURRENCY,Path(file.filename or 'PO').name,text,path,digest,mime,now,
                 fields.get('vendor_company_name'),fields.get('vendor_address'),fields.get('vendor_tax_id'),fields.get('buyer_company_name'),fields.get('buyer_address'),fields.get('buyer_tax_id'),
                 fields.get('subtotal'),fields.get('discount'),fields.get('tax'),fields.get('shipping'),fields.get('other_charges'),fields.get('rounding'),
                 json.dumps(fields,ensure_ascii=False),fields.get('currency_confidence'),total_status,total_source))
            _write_po_match(conn, poid, status=vendor_resolution['status'], method=vendor_resolution.get('method'),
                            reason=vendor_resolution['reason'], vendor_id=vendor['id'] if vendor else None,
                            linked_by=user['id'] if vendor else None, now=now)
            for idx,it in enumerate(items, start=1):
                conn.execute('''INSERT INTO purchase_order_items
                    (id,po_id,item_name,quantity,unit,unit_price,tax_rate,item_tax,line_total,sku,description,item_discount,item_discount_source,line_number)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                    (str(uuid.uuid4()),poid,it['item_name'],it.get('quantity'),it.get('unit'),it.get('unit_price'),it.get('tax_rate'),it.get('item_tax'),it.get('line_total'),
                     it.get('sku'),it.get('description'),it.get('item_discount'),it.get('item_discount_source'),idx))
            audit(conn,'PO_UPLOADED','SUCCESS',actor=user,reason='PO uploaded; extraction provenance and financial completeness recorded.',details={
                'po_number':fields['purchase_order_number'],'vendor_id':vendor['id'] if vendor else None,
                'extracted_supplier_name':fields.get('vendor_company_name'),'extracted_supplier_tax_id':fields.get('vendor_tax_id'),
                'vendor_link_status':vendor_resolution['status'],'vendor_match_method':vendor_resolution.get('method'),
                'vendor_match_reason':vendor_resolution['reason'],'document_hash':digest,'mime':mime,
                'total_status':total_status,'item_count':len(items),'extraction_source':extraction_engine,
                'document_type':extraction_trace.get('document_type'),'provider':extraction_trace.get('provider'),
                'provider_host':extraction_trace.get('provider_host'),'request_status':extraction_trace.get('request_status'),
                'fallback_used':bool(extraction_trace.get('fallback_used')),'retry_count':int(extraction_trace.get('retry_count') or 0),
                'latency_ms':extraction_trace.get('latency_ms'),'error_type':extraction_trace.get('error_type')})
        request.session['flash_success']='PO uploaded successfully. Review the vendor and total if the extraction needs confirmation.'
    except ValueError as exc:
        request.session['flash_error']=str(exc)
    finally:
        await file.close()
    return redirect('/purchase-orders')

@app.get('/po-documents/{po_id}')
def get_po_document(request: Request, po_id: str):
    user=user_or_redirect(request)
    if not isinstance(user,dict): return user
    with get_db() as conn:
        po=conn.execute('SELECT * FROM purchase_orders WHERE id=?',(po_id,)).fetchone()
    if not po or not po['document_path']:
        raise HTTPException(404,'The source PO file is unavailable.')
    path=Path(po['document_path']).resolve()
    if UPLOAD_DIR.resolve() not in path.parents or not path.exists():
        raise HTTPException(404,'PO file not found.')
    return FileResponse(path,filename=Path(po['source_filename'] or 'purchase-order.pdf').name,
        media_type=po['mime_type'] or 'application/octet-stream',headers={'Cache-Control':'private, no-store','X-Content-Type-Options':'nosniff'})

@app.get('/audits',response_class=HTMLResponse)
def audits_page(request: Request,q:str=''):
    user=user_or_redirect(request)
    if not isinstance(user,dict): return user
    with get_db() as conn:
        if q.strip():
            like=f'%{q.strip()}%'
            events=conn.execute('''SELECT a.*,u.name actor_name FROM audit_events a LEFT JOIN users u ON u.id=a.actor_id
                WHERE a.event_type LIKE ? OR a.event_reason LIKE ? OR a.invoice_id LIKE ? ORDER BY a.created_at DESC, a.rowid DESC LIMIT 300''',(like,like,like)).fetchall()
        else:
            events=conn.execute('''SELECT a.*,u.name actor_name FROM audit_events a LEFT JOIN users u ON u.id=a.actor_id ORDER BY a.created_at DESC, a.rowid DESC LIMIT 300''').fetchall()
        events=prepare_audit_rows(events)
    return render(request,'audits.html',{'events':events,'q':q})

@app.get('/export/audit.xlsx')
def export_audit(request: Request):
    user=user_or_redirect(request)
    if not isinstance(user,dict): return user
    if not require_roles(user,'finance','supervisor','admin'): raise HTTPException(403,'You are not authorized to export the audit report.')
    with get_db() as conn:
        audit(conn,'AUDIT_EXPORTED','SUCCESS',actor=user,reason='User exported the Excel audit report.')
        workbook=build_audit_workbook(conn)
    return StreamingResponse(workbook,media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',headers={'Content-Disposition':'attachment; filename="sentrion-audit.xlsx"','Cache-Control':'no-store'})

@app.get('/documents/{document_id}')
def get_document(request: Request,document_id:str):
    user=user_or_redirect(request)
    if not isinstance(user,dict): return user
    with get_db() as conn:
        doc=conn.execute('''SELECT d.*,i.id invoice_id FROM invoice_documents d JOIN invoices i ON i.id=d.invoice_id WHERE d.id=?''',(document_id,)).fetchone()
    if not doc or not doc['stored_path']:
        raise HTTPException(404,'The original file is unavailable for demo data or is no longer available.')
    path=Path(doc['stored_path']).resolve()
    if UPLOAD_DIR.resolve() not in path.parents or not path.exists():
        raise HTTPException(404,'Document file not found.')
    return FileResponse(path,filename=Path(doc['original_filename']).name,media_type=mimetypes.guess_type(doc['original_filename'])[0] or 'application/octet-stream',headers={'Cache-Control':'private, no-store','X-Content-Type-Options':'nosniff'})

@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    if exc.status_code in (401,403,404,409,400) and request.url.path not in {'/login','/logout'} and request.headers.get('accept','').find('text/html')>=0:
        return render(request,'error.html',{'status_code':exc.status_code,'message':str(exc.detail)},status_code=exc.status_code)
    from fastapi.responses import JSONResponse
    return JSONResponse(status_code=exc.status_code,content={'detail':exc.detail},headers=exc.headers)
