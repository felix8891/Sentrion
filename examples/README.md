# Sentrion Example Documents

All files in this directory are **fictional USD-denominated documents** intended for local testing only. Company identities, addresses, tax identifiers, bank names, and account numbers are synthetic.

- `invoices/01_invoice_green.pdf` — normal invoice aligned with a verified vendor and PO; intended for GREEN.
- `invoices/02_invoice_yellow_new_account.pdf` — known vendor with an unverified payment destination; intended for YELLOW.
- `invoices/03_invoice_red_prompt_injection.pdf` — contains an AI-directed malicious instruction; intended for RED.
- `invoices/04_invoice_po_mismatch.pdf` — adds a large line item absent from the matched PO; intended to trigger a RED PO-total mismatch.
- `invoices/05_invoice_scanned_yellow.png` — scanned-style USD invoice image for OCR testing.
- `purchase_orders/01_purchase_order_upload_example.pdf` — USD purchase-order document for the upload workflow.

The examples contain intentionally inconsistent data where required to demonstrate security controls. They are not genuine business documents.

## Updating an existing local demo database

For an existing installation whose database already contains the earlier Indonesian/IDR seeded records, stop the Sentrion server and run the migration from the `Sentrion_MVP` folder:

```powershell
python tools_migrate_demo_data_to_english_usd.py
python tools_migrate_demo_data_to_english_usd.py --apply
```

The first command is a dry run. The second creates a timestamped database backup and updates only the known seeded demo vendor, invoice, purchase-order, and related line-item records. It preserves user-created records and historical audit events, appending new audit events for the migration. Do not run the migration while the server is active. If pending dual-authorization requests reference demo vendors or invoices, resolve them before applying the migration.

## Currency handling

Sentrion uses a shared currency formatter and reads explicit currency evidence from invoice/PO text where available. USD is the configured fallback for documents without a clear currency marker; these records are marked with low currency confidence and the reviewer is prompted to confirm the currency. Currency symbols such as `$` can be ambiguous, so currency confirmation remains a human responsibility. Invoice/PO records with differing currency codes are flagged RED and monetary amount comparisons are skipped; the application does not apply an implicit exchange rate.

The PO mismatch threshold is a demonstration rule: for IDR it is the greater of 10% of the PO total or IDR 500,000; for other currencies it is 10% of the PO total. Validate thresholds against company policy before production use.
