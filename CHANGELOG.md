# Changelog — Vendor Resolution & PO Relinking

## 2026-10-20 — Local revision

- Centralized PO/vendor identity classification and normalization.
- Limited automatic trusted links to unique exact normalized tax-ID matches against verified vendors.
- Kept name-only matches as candidates requiring authorized review.
- Preserved conflicting tax IDs as identity conflicts and RED findings.
- Added idempotent reconciliation of older unresolved POs after vendor verification.
- Added role-protected PO link/review routes and audit records.
- Reassessed related invoices after successful linking while retaining prior findings as `SUPERSEDED` history.
- Added PO match provenance columns through a safe SQLite forward migration.
- Added purchase-order detail view fields for extracted identity, trusted link, matching reason, line items, financial summary, provenance, and audit history.
- Added regression tests for matching, relinking, idempotency, risk reassessment, conflicting IDs, role authorization, and schema migration.

No Vercel deployment or production database changes were performed.
