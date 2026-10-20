# SENTRION_DOC_000001.pdf — Extraction Regression Diagnostic

## Baseline observation

The original local extractor was executed against the supplied PDF using PyMuPDF native text extraction. The PDF produced 1,486 characters of selectable text and included all invoice fields and all eight line-item rows. The parser output nevertheless had `vendor_company_name=null`, `vendor_address=null`, `purchase_order_number=null`, `discount=null`, `tax=1.0`, and `items=[]`.

## Root cause

1. Supplier and BILL TO are section headers on their own lines, rather than `Supplier: Name` labels. The old parser did not read those blocks.
2. PURCHASE ORDER is a separate heading, with its value on the following line; the old pattern required the PO label/value to appear on one line.
3. Summary amounts are laid out as a label line followed by a monetary-value line. Subtotal and total were captured through loose matching, while discount and GST total were not reliably handled. The broad tax regex could pick up a numeric fragment from the tax-registration area and return 1.0.
4. The table is represented in PDF text as a sequence of separate cell lines after the column headers. The old line-item parser only handled synthetic `ITEM: ... | QTY: ...` rows, so all eight rows were lost.
5. Re-analysis was queued as a background task, but the route immediately flashed that the document was reprocessed. This could present a success message before the final processing state had been rendered.

## Corrected output verified

- Native PDF text is used first; OCR is only a fallback for sparse/unusable text layers.
- Supplier and buyer names, addresses, and tax IDs are separated by section context.
- Invoice/PO references, payment terms/reference, currency, date and summary values are extracted.
- Exactly 8 line items are extracted in source order with SKU, unit, quantity, unit price, printed line total, printed discount and printed GST amount. Tax rate remains null because the source prints GST amounts, not a per-item rate.
- No bank name/account is fabricated, because the document does not contain those details.
- Extraction is persisted in the invoice record and structured JSON; SKU, line discount and line order are persisted in `invoice_items`.
- An unmatched vendor name is preserved with `vendor_id` null. Name extraction does not mark the supplier as verified.
- Re-analysis displays a queued message and the detail page polls while the job is queued/processing. The UI is not told that work completed just because it was scheduled.

## Verification

The regression suite includes the supplied PDF as a fixture and exercises both the parser and full application upload/persistence workflow. See the test command in the project README.
