import sqlite3
from pathlib import Path

from app import db


def test_vendor_management_migration_backs_up_and_preserves_existing_data(tmp_path, monkeypatch):
    legacy_db = tmp_path / 'legacy_sentrion.db'
    with sqlite3.connect(legacy_db) as conn:
        conn.executescript(db.SCHEMA)
        conn.execute('''INSERT INTO vendors
            (id,vendor_code,company_name,address,tax_id,verification_status,created_at,updated_at)
            VALUES ('vendor-legacy','LEGACY-001','Legacy Supplier Ltd.','Old address','TAX-LEGACY','UNVERIFIED','2026-01-01','2026-01-01')''')
    monkeypatch.setattr(db, 'DB_PATH', legacy_db)
    db.init_db()
    backups = list(tmp_path.glob('legacy_sentrion.before_vendor_management_*.db.bak'))
    assert len(backups) == 1
    with sqlite3.connect(legacy_db) as conn:
        vendor_cols = {r[1] for r in conn.execute('PRAGMA table_info(vendors)').fetchall()}
        bank_cols = {r[1] for r in conn.execute('PRAGMA table_info(vendor_bank_accounts)').fetchall()}
        vendor = conn.execute("SELECT company_name,address FROM vendors WHERE id='vendor-legacy'").fetchone()
    assert {'country','contact_person','email','phone'} <= vendor_cols
    assert {'bank_country','swift_bic'} <= bank_cols
    assert vendor == ('Legacy Supplier Ltd.','Old address')
    with sqlite3.connect(backups[0]) as conn:
        old_cols = {r[1] for r in conn.execute('PRAGMA table_info(vendors)').fetchall()}
        old_vendor = conn.execute("SELECT company_name FROM vendors WHERE id='vendor-legacy'").fetchone()
    assert 'country' not in old_cols
    assert old_vendor == ('Legacy Supplier Ltd.',)


def test_po_vendor_link_migration_preserves_existing_relationships(tmp_path, monkeypatch):
    legacy_db = tmp_path / 'legacy_po_links.db'
    legacy_schema = db.SCHEMA.replace(
        ",\n vendor_match_status TEXT NOT NULL DEFAULT 'UNMATCHED', vendor_match_method TEXT, vendor_match_reason TEXT, vendor_match_at TEXT, vendor_linked_by TEXT",
        "",
    )
    with sqlite3.connect(legacy_db) as conn:
        conn.executescript(legacy_schema)
        conn.execute('''INSERT INTO vendors
            (id,vendor_code,company_name,tax_id,verification_status,created_at,updated_at)
            VALUES ('vendor-po-migration','VM-001','Migration Test Vendor Ltd.','VM-TAX-001','VERIFIED','2026-01-01','2026-01-01')''')
        conn.execute('''INSERT INTO purchase_orders
            (id,po_number,vendor_id,total,currency,status,created_at,vendor_company_name,vendor_tax_id,total_status,total_source)
            VALUES ('po-migration','PO-MIGRATION-001','vendor-po-migration',100,'USD','ACTIVE','2026-01-01','Migration Test Vendor Ltd.','VM-TAX-001','EXTRACTED','LEGACY')''')
    monkeypatch.setattr(db, 'DB_PATH', legacy_db)
    db.init_db()
    with sqlite3.connect(legacy_db) as conn:
        conn.row_factory = sqlite3.Row
        po = conn.execute("SELECT * FROM purchase_orders WHERE id='po-migration'").fetchone()
        columns = {row['name'] for row in conn.execute('PRAGMA table_info(purchase_orders)').fetchall()}
    assert {'vendor_match_status','vendor_match_method','vendor_match_reason','vendor_match_at','vendor_linked_by'} <= columns
    assert po['vendor_id'] == 'vendor-po-migration'
    assert po['po_number'] == 'PO-MIGRATION-001'
    assert po['vendor_match_status'] == 'MATCHED'
    assert po['vendor_match_method'] == 'LEGACY_EXISTING_LINK'
    assert po['total'] == 100
    assert len(list(tmp_path.glob('legacy_po_links.before_vendor_management_*.db.bak'))) == 1
