from __future__ import annotations
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from .config import DB_PATH

SCHEMA = r'''
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS users (
 id TEXT PRIMARY KEY, name TEXT NOT NULL, email TEXT NOT NULL UNIQUE,
 password_hash TEXT NOT NULL, role TEXT NOT NULL CHECK(role IN ('finance','supervisor','admin')),
 is_active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS vendors (
 id TEXT PRIMARY KEY, vendor_code TEXT NOT NULL UNIQUE, company_name TEXT NOT NULL,
 address TEXT, tax_id TEXT, verification_status TEXT NOT NULL DEFAULT 'UNVERIFIED',
 current_bank_name TEXT, current_bank_account_number TEXT, current_bank_account_holder TEXT,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS vendor_bank_accounts (
 id TEXT PRIMARY KEY, vendor_id TEXT NOT NULL REFERENCES vendors(id), bank_name TEXT NOT NULL,
 account_number TEXT NOT NULL, account_holder_name TEXT, status TEXT NOT NULL,
 change_request_id TEXT, created_at TEXT NOT NULL, verified_at TEXT, verified_by TEXT
);
CREATE INDEX IF NOT EXISTS idx_bank_vendor_status ON vendor_bank_accounts(vendor_id, status);
CREATE TABLE IF NOT EXISTS purchase_orders (
 id TEXT PRIMARY KEY, po_number TEXT NOT NULL UNIQUE, vendor_id TEXT REFERENCES vendors(id),
 issue_date TEXT, total REAL NOT NULL DEFAULT 0, currency TEXT NOT NULL DEFAULT 'USD',
 source_filename TEXT, source_text TEXT, document_path TEXT, document_hash TEXT, mime_type TEXT,
 status TEXT NOT NULL DEFAULT 'ACTIVE', created_at TEXT NOT NULL,
 vendor_company_name TEXT, vendor_address TEXT, vendor_tax_id TEXT, buyer_company_name TEXT, buyer_address TEXT, buyer_tax_id TEXT,
 subtotal REAL, discount REAL, tax REAL, shipping REAL, other_charges REAL, rounding REAL,
 extracted_json TEXT, currency_confidence TEXT, total_status TEXT NOT NULL DEFAULT 'UNKNOWN', total_source TEXT,
 vendor_match_status TEXT NOT NULL DEFAULT 'UNMATCHED', vendor_match_method TEXT, vendor_match_reason TEXT, vendor_match_at TEXT, vendor_linked_by TEXT
);
CREATE TABLE IF NOT EXISTS purchase_order_items (
 id TEXT PRIMARY KEY, po_id TEXT NOT NULL REFERENCES purchase_orders(id) ON DELETE CASCADE,
 item_name TEXT NOT NULL, quantity REAL, unit TEXT, unit_price REAL, tax_rate REAL, item_tax REAL, line_total REAL,
 sku TEXT, description TEXT, item_discount REAL, item_discount_source TEXT, line_number INTEGER
);
CREATE TABLE IF NOT EXISTS invoices (
 id TEXT PRIMARY KEY, invoice_receive_date TEXT NOT NULL, invoice_issue_date TEXT, invoice_number TEXT,
 vendor_id TEXT REFERENCES vendors(id), vendor_company_name TEXT, vendor_address TEXT, vendor_tax_id TEXT,
 purchase_order_number TEXT, due_date TEXT, currency TEXT NOT NULL DEFAULT 'USD', bank_name TEXT,
 bank_account_number TEXT, bank_account_holder_name TEXT, subtotal REAL, discount REAL, tax REAL,
 total REAL, extraction_status TEXT NOT NULL DEFAULT 'QUEUED', processing_status TEXT NOT NULL DEFAULT 'QUEUED',
 verification_status TEXT NOT NULL DEFAULT 'PENDING', approval_status TEXT NOT NULL DEFAULT 'NOT_REQUESTED',
 payment_status TEXT NOT NULL DEFAULT 'NOT_EXECUTED', risk_level TEXT NOT NULL DEFAULT 'YELLOW',
 risk_reasons_json TEXT NOT NULL DEFAULT '[]', source_filename TEXT NOT NULL, stored_path TEXT,
 document_hash TEXT NOT NULL, raw_text TEXT, extracted_json TEXT, created_by TEXT REFERENCES users(id),
 verification_evidence TEXT, verified_by TEXT REFERENCES users(id), verified_at TEXT,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_invoices_vendor ON invoices(vendor_id, invoice_number);
CREATE INDEX IF NOT EXISTS idx_invoices_receive ON invoices(invoice_receive_date);
CREATE INDEX IF NOT EXISTS idx_invoices_risk ON invoices(risk_level, verification_status, approval_status);
CREATE TABLE IF NOT EXISTS invoice_items (
 id TEXT PRIMARY KEY, invoice_id TEXT NOT NULL REFERENCES invoices(id) ON DELETE CASCADE,
 item_name TEXT NOT NULL, description TEXT, quantity REAL, unit TEXT, unit_price REAL,
 tax_rate REAL, item_tax REAL, line_total REAL, sku TEXT, item_discount REAL, item_discount_source TEXT, line_number INTEGER
);
CREATE TABLE IF NOT EXISTS invoice_documents (
 id TEXT PRIMARY KEY, invoice_id TEXT NOT NULL REFERENCES invoices(id) ON DELETE CASCADE,
 original_filename TEXT NOT NULL, stored_path TEXT, sha256 TEXT NOT NULL,
 byte_size INTEGER NOT NULL DEFAULT 0, mime_type TEXT, uploaded_by TEXT REFERENCES users(id),
 uploaded_at TEXT NOT NULL, is_original INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS risk_findings (
 id TEXT PRIMARY KEY, invoice_id TEXT NOT NULL REFERENCES invoices(id) ON DELETE CASCADE,
 finding_code TEXT NOT NULL, severity TEXT NOT NULL, title TEXT NOT NULL, detail TEXT NOT NULL,
 expected_value TEXT, observed_value TEXT, evidence_source TEXT, status TEXT NOT NULL DEFAULT 'ACTIVE',
 analysis_run_id TEXT NOT NULL, created_at TEXT NOT NULL, resolved_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_findings_invoice_status ON risk_findings(invoice_id, status);
CREATE TABLE IF NOT EXISTS approval_requests (
 id TEXT PRIMARY KEY, request_type TEXT NOT NULL, entity_type TEXT NOT NULL, entity_id TEXT NOT NULL,
 status TEXT NOT NULL, requested_by TEXT NOT NULL REFERENCES users(id), first_approval_at TEXT,
 approved_by TEXT REFERENCES users(id), approved_at TEXT, rejected_by TEXT REFERENCES users(id),
 rejected_at TEXT, reason TEXT NOT NULL, evidence TEXT NOT NULL, context_json TEXT NOT NULL DEFAULT '{}',
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS approval_records (
 id TEXT PRIMARY KEY, request_id TEXT NOT NULL REFERENCES approval_requests(id), user_id TEXT NOT NULL REFERENCES users(id),
 decision TEXT NOT NULL, reason TEXT, created_at TEXT NOT NULL, UNIQUE(request_id, user_id)
);
CREATE TABLE IF NOT EXISTS approval_attachments (
 id TEXT PRIMARY KEY, request_id TEXT NOT NULL REFERENCES approval_requests(id) ON DELETE RESTRICT,
 category TEXT NOT NULL, original_filename TEXT NOT NULL, stored_filename TEXT NOT NULL, sha256 TEXT NOT NULL,
 byte_size INTEGER NOT NULL, mime_type TEXT NOT NULL, uploaded_by TEXT NOT NULL REFERENCES users(id), uploaded_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_approval_attachments_request ON approval_attachments(request_id, category, uploaded_at);
CREATE TABLE IF NOT EXISTS payments (
 id TEXT PRIMARY KEY, invoice_id TEXT NOT NULL REFERENCES invoices(id), idempotency_key TEXT NOT NULL UNIQUE,
 status TEXT NOT NULL, simulated_reference TEXT, initiated_by TEXT NOT NULL REFERENCES users(id),
 amount REAL NOT NULL, currency TEXT NOT NULL DEFAULT 'USD', created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_payments_invoice ON payments(invoice_id);
CREATE TABLE IF NOT EXISTS audit_events (
 id TEXT PRIMARY KEY, invoice_id TEXT REFERENCES invoices(id), actor_id TEXT REFERENCES users(id),
 actor_role TEXT, event_type TEXT NOT NULL, event_result TEXT NOT NULL, risk_level_at_event TEXT,
 event_reason TEXT, details_json TEXT NOT NULL DEFAULT '{}', related_approval_id TEXT,
 related_document_hash TEXT, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_invoice_time ON audit_events(invoice_id, created_at);
CREATE INDEX IF NOT EXISTS idx_audit_time ON audit_events(created_at);
'''

def utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()

@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys = ON')
    conn.execute('PRAGMA busy_timeout = 15000')
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    _backup_before_vendor_migration()
    with get_db() as conn:
        conn.executescript(SCHEMA)
        # Lightweight forward-compatible migrations for existing local demo databases.
        existing = {row['name'] for row in conn.execute('PRAGMA table_info(purchase_orders)').fetchall()}
        for name, sql_type in [
            ('document_path','TEXT'),('document_hash','TEXT'),('mime_type','TEXT'),
            ('vendor_company_name','TEXT'),('vendor_address','TEXT'),('vendor_tax_id','TEXT'),
            ('buyer_company_name','TEXT'),('buyer_address','TEXT'),('buyer_tax_id','TEXT'),('subtotal','REAL'),('discount','REAL'),
            ('tax','REAL'),('shipping','REAL'),('other_charges','REAL'),('rounding','REAL'),
            ('extracted_json','TEXT'),('currency_confidence','TEXT'),
            ('total_status',"TEXT NOT NULL DEFAULT 'UNKNOWN'"),('total_source','TEXT'),
            ('vendor_match_status',"TEXT NOT NULL DEFAULT 'UNMATCHED'"),('vendor_match_method','TEXT'),
            ('vendor_match_reason','TEXT'),('vendor_match_at','TEXT'),('vendor_linked_by','TEXT')
        ]:
            if name not in existing:
                conn.execute(f'ALTER TABLE purchase_orders ADD COLUMN {name} {sql_type}')
        # Existing positive totals predate explicit provenance and are retained as legacy values.
        conn.execute("UPDATE purchase_orders SET total_status='EXTRACTED', total_source=COALESCE(total_source,'LEGACY_STORED_VALUE') WHERE total_status='UNKNOWN' AND total>0")
        conn.execute("UPDATE purchase_orders SET total_status='MISSING', total_source='MISSING' WHERE total_status='UNKNOWN' AND COALESCE(total,0)=0")
        conn.execute("UPDATE purchase_orders SET vendor_match_status='MATCHED', vendor_match_method=COALESCE(vendor_match_method,'LEGACY_EXISTING_LINK'), vendor_match_reason=COALESCE(vendor_match_reason,'Existing PO-to-vendor relationship retained during schema migration.'), vendor_match_at=COALESCE(vendor_match_at,created_at) WHERE vendor_id IS NOT NULL AND (vendor_match_status IS NULL OR vendor_match_status='UNMATCHED')")
        invoice_item_columns = {row['name'] for row in conn.execute('PRAGMA table_info(invoice_items)').fetchall()}
        if 'item_discount_source' not in invoice_item_columns:
            conn.execute('ALTER TABLE invoice_items ADD COLUMN item_discount_source TEXT')
        po_item_columns = {row['name'] for row in conn.execute('PRAGMA table_info(purchase_order_items)').fetchall()}
        for name, sql_type in [('sku','TEXT'),('description','TEXT'),('item_discount','REAL'),('item_discount_source','TEXT'),('line_number','INTEGER')]:
            if name not in po_item_columns:
                conn.execute(f'ALTER TABLE purchase_order_items ADD COLUMN {name} {sql_type}')
        # Additional vendor master fields are nullable to preserve existing records.
        vendor_columns = {row['name'] for row in conn.execute('PRAGMA table_info(vendors)').fetchall()}
        for name, sql_type in [
            ('country', 'TEXT'), ('contact_person', 'TEXT'), ('email', 'TEXT'), ('phone', 'TEXT')
        ]:
            if name not in vendor_columns:
                conn.execute(f'ALTER TABLE vendors ADD COLUMN {name} {sql_type}')
        bank_columns = {row['name'] for row in conn.execute('PRAGMA table_info(vendor_bank_accounts)').fetchall()}
        for name, sql_type in [('bank_country', 'TEXT'), ('swift_bic', 'TEXT')]:
            if name not in bank_columns:
                conn.execute(f'ALTER TABLE vendor_bank_accounts ADD COLUMN {name} {sql_type}')
        # Preserve supplier SKUs and per-line discounts for line items extracted from
        # real-world tables. Existing records remain intact and nullable new columns
        # keep older demo seed INSERT statements backward compatible.
        item_columns = {row['name'] for row in conn.execute('PRAGMA table_info(invoice_items)').fetchall()}
        for name, sql_type in [('sku','TEXT'),('item_discount','REAL'),('line_number','INTEGER')]:
            if name not in item_columns:
                conn.execute(f'ALTER TABLE invoice_items ADD COLUMN {name} {sql_type}')

def _backup_before_vendor_migration():
    """Back up an existing SQLite database before adding vendor-management columns."""
    if not DB_PATH.exists() or DB_PATH.stat().st_size == 0:
        return
    source = sqlite3.connect(str(DB_PATH), timeout=15)
    try:
        tables = {row[0] for row in source.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        if not tables:
            return
        # Missing vendor tables also mean the upcoming SCHEMA execution will alter a
        # non-empty existing database, so back it up rather than assuming it is empty.
        if 'vendors' not in tables or 'vendor_bank_accounts' not in tables or 'approval_attachments' not in tables:
            needs_backup = True
        else:
            vendor_columns = {row[1] for row in source.execute('PRAGMA table_info(vendors)').fetchall()}
            bank_columns = {row[1] for row in source.execute('PRAGMA table_info(vendor_bank_accounts)').fetchall()}
            po_columns = {row[1] for row in source.execute('PRAGMA table_info(purchase_orders)').fetchall()} if 'purchase_orders' in tables else set()
            needs_backup = bool(
                {'country','contact_person','email','phone'} - vendor_columns
                or {'bank_country','swift_bic'} - bank_columns
                or {'vendor_match_status','vendor_match_method','vendor_match_reason','vendor_match_at','vendor_linked_by'} - po_columns
            )
        if not needs_backup:
            return
        stamp = datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')
        backup_path = DB_PATH.with_name(f'{DB_PATH.stem}.before_vendor_management_{stamp}.db.bak')
        with sqlite3.connect(str(backup_path)) as destination:
            source.backup(destination)
    finally:
        source.close()
