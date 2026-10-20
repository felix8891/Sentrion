"""Deterministic, auditable vendor identity resolution for purchase orders.

An extracted document name is evidence, not proof of vendor verification. Automatic
linking is limited to a unique, exact normalized tax-ID match against an already
verified vendor. Exact name-only matches remain candidates until an authorized
reviewer confirms them.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Any

from ..db import utcnow
from .audit import audit


def normalize_tax_id(value: str | None) -> str:
    """Normalize separators/case while preserving meaningful letters and leading zeros."""
    if not value:
        return ""
    value = unicodedata.normalize("NFKC", str(value)).casefold()
    return re.sub(r"[^a-z0-9]", "", value)


def normalize_legal_name(value: str | None) -> str:
    """Exact normalized name comparison; does not use fuzzy/substring matching."""
    if not value:
        return ""
    value = unicodedata.normalize("NFKD", str(value)).casefold()
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    return " ".join(re.findall(r"[a-z0-9]+", value))


def _vendor_rows(conn):
    return conn.execute("SELECT * FROM vendors ORDER BY company_name, id").fetchall()


def _write_po_match(conn, po_id: str, *, status: str, method: str | None,
                    reason: str, vendor_id: str | None = None,
                    linked_by: str | None = None, now: str | None = None) -> None:
    now = now or utcnow()
    conn.execute(
        """UPDATE purchase_orders
           SET vendor_id=?, vendor_match_status=?, vendor_match_method=?,
               vendor_match_reason=?, vendor_match_at=?, vendor_linked_by=?
           WHERE id=?""",
        (vendor_id, status, method, reason, now if status == "MATCHED" else None,
         linked_by if status == "MATCHED" else None, po_id),
    )


def classify_po_vendor_identity(conn, *, supplier_name: str | None,
                                supplier_tax_id: str | None,
                                vendors: list[Any] | None = None) -> dict[str, Any]:
    """Classify an extracted identity without silently trusting name-only matches."""
    vendors = vendors if vendors is not None else _vendor_rows(conn)
    po_tax = normalize_tax_id(supplier_tax_id)
    po_name = normalize_legal_name(supplier_name)

    tax_matches = [v for v in vendors if po_tax and normalize_tax_id(v["tax_id"]) == po_tax]
    if len(tax_matches) == 1:
        vendor = tax_matches[0]
        if vendor["verification_status"] == "VERIFIED":
            return {"status": "MATCHED", "vendor": vendor, "method": "EXACT_TAX_ID",
                    "reason": "The normalized supplier tax ID exactly matches a verified vendor record."}
        return {"status": "CANDIDATE", "vendor": vendor, "method": "EXACT_TAX_ID_UNVERIFIED",
                "reason": "The tax ID matches a vendor record, but that vendor is not verified."}
    if len(tax_matches) > 1:
        return {"status": "IDENTITY_CONFLICT", "vendor": None, "method": None,
                "reason": "More than one vendor record has the same normalized tax ID; manual review is required."}

    name_matches = [v for v in vendors if po_name and normalize_legal_name(v["company_name"]) == po_name]
    if po_tax and name_matches:
        contradictory = [v for v in name_matches if normalize_tax_id(v["tax_id"]) and normalize_tax_id(v["tax_id"]) != po_tax]
        if contradictory:
            return {"status": "IDENTITY_CONFLICT", "vendor": None, "method": "NAME_MATCH_TAX_ID_CONFLICT",
                    "reason": "The exact supplier name matches a vendor record, but the reliable supplier tax IDs conflict."}

    if len(name_matches) == 1:
        vendor = name_matches[0]
        return {"status": "CANDIDATE", "vendor": vendor, "method": "EXACT_NAME_CANDIDATE",
                "reason": "The normalized supplier name matches exactly, but a trusted link requires authorized identity confirmation because an exact verified tax-ID match is unavailable."}
    if len(name_matches) > 1:
        return {"status": "CANDIDATE", "vendor": None, "method": "AMBIGUOUS_NAME_CANDIDATES",
                "reason": "Multiple vendor records have the same normalized supplier name; choose only after reviewing reliable evidence."}
    return {"status": "UNMATCHED", "vendor": None, "method": None,
            "reason": "No exact tax-ID or normalized legal-name candidate was found in the vendor master."}


def update_unlinked_po_candidate_state(conn, po_id: str, *, actor: dict | None = None) -> dict[str, Any]:
    po = conn.execute("SELECT * FROM purchase_orders WHERE id=?", (po_id,)).fetchone()
    if not po:
        raise ValueError("Purchase order not found.")
    if po["vendor_id"]:
        vendor = conn.execute("SELECT * FROM vendors WHERE id=?", (po["vendor_id"],)).fetchone()
        if vendor:
            _write_po_match(conn, po_id, status="MATCHED", method=po["vendor_match_method"] or "LEGACY_EXISTING_LINK",
                            reason=po["vendor_match_reason"] or "Existing PO vendor relationship retained.",
                            vendor_id=vendor["id"], linked_by=po["vendor_linked_by"], now=po["vendor_match_at"] or utcnow())
            return {"status": "MATCHED", "vendor": vendor, "method": po["vendor_match_method"] or "LEGACY_EXISTING_LINK",
                    "reason": po["vendor_match_reason"] or "Existing PO vendor relationship retained."}
    result = classify_po_vendor_identity(conn, supplier_name=po["vendor_company_name"], supplier_tax_id=po["vendor_tax_id"])
    if result["status"] == "MATCHED" and result.get("vendor"):
        link_result = link_po_to_vendor(conn, po_id=po_id, vendor_id=result["vendor"]["id"], actor=actor,
                                        reason="Authorized safe reconciliation: exact normalized tax ID match.")
        return {**result, **link_result}
    _write_po_match(conn, po_id, status=result["status"], method=result["method"], reason=result["reason"], vendor_id=None)
    return result


def _reassess_related_invoices(conn, po, *, actor=None, reason: str) -> int:
    """Recalculate invoices referencing the PO and preserve findings as superseded history."""
    from .risk import evaluate_invoice
    rows = conn.execute(
        "SELECT id FROM invoices WHERE lower(trim(COALESCE(purchase_order_number,'')))=lower(trim(?))",
        (po["po_number"],),
    ).fetchall()
    for row in rows:
        result = evaluate_invoice(conn, row["id"])
        current = conn.execute("SELECT risk_level FROM invoices WHERE id=?", (row["id"],)).fetchone()
        audit(conn, "RISK_REASSESSED_AFTER_PO_VENDOR_LINK", "COMPLETED", actor=actor,
              invoice_id=row["id"], reason=reason,
              risk_level=current["risk_level"] if current else result.get("risk_level"),
              document_hash=po["document_hash"],
              details={"po_number": po["po_number"], "vendor_id": po["vendor_id"],
                       "analysis_run_id": result.get("analysis_run_id")})
    return len(rows)


def link_po_to_vendor(conn, *, po_id: str, vendor_id: str, actor: dict | None,
                      reason: str, confirm_name_only: bool = False,
                      override_tax_conflict: bool = False) -> dict[str, Any]:
    """Create an explicit authorized relationship after validating identity evidence."""
    po = conn.execute("SELECT * FROM purchase_orders WHERE id=?", (po_id,)).fetchone()
    if not po:
        raise ValueError("Purchase order not found.")
    vendor = conn.execute("SELECT * FROM vendors WHERE id=?", (vendor_id,)).fetchone()
    if not vendor:
        raise ValueError("Selected vendor was not found.")
    if vendor["verification_status"] != "VERIFIED":
        raise ValueError("Only a verified vendor can be linked as a trusted vendor.")
    if po["vendor_id"] and po["vendor_id"] == vendor_id:
        return {"status": "ALREADY_MATCHED", "vendor_id": vendor_id, "reassessed_invoices": 0}
    if po["vendor_id"] and po["vendor_id"] != vendor_id:
        raise ValueError("This PO is already linked to a different vendor. Review the existing relationship before changing it.")

    po_tax, vendor_tax = normalize_tax_id(po["vendor_tax_id"]), normalize_tax_id(vendor["tax_id"])
    name_match = bool(normalize_legal_name(po["vendor_company_name"]) and
                      normalize_legal_name(po["vendor_company_name"]) == normalize_legal_name(vendor["company_name"]))
    if po_tax and vendor_tax and po_tax == vendor_tax:
        method = "EXACT_TAX_ID"
        detail = "Trusted vendor linked after exact normalized tax-ID verification."
    elif po_tax and vendor_tax and po_tax != vendor_tax:
        if not override_tax_conflict or len((reason or "").strip()) < 20:
            raise ValueError("The PO and vendor have conflicting tax IDs. A supervisor/admin must explicitly review the conflict with a detailed reason; do not use a name-only link.")
        method = "AUTHORIZED_IDENTITY_CONFLICT_OVERRIDE"
        detail = "A supervisor/admin explicitly overrode a conflicting tax-ID relationship; the tax conflict remains a risk finding."
    else:
        if not name_match:
            raise ValueError("The PO supplier name does not exactly match the selected vendor and no exact tax-ID match is available.")
        if not confirm_name_only or len((reason or "").strip()) < 10:
            raise ValueError("An exact name-only match requires explicit identity confirmation and a review reason of at least 10 characters.")
        method = "AUTHORIZED_NAME_CONFIRMATION"
        detail = "A supervisor/admin confirmed the exact normalized supplier-name match because a reliable exact tax-ID match was unavailable."

    now = utcnow()
    _write_po_match(conn, po_id, status="MATCHED", method=method, reason=detail + " Review note: " + reason.strip(),
                    vendor_id=vendor_id, linked_by=(actor or {}).get("id"), now=now)
    audit(conn, "PO_VENDOR_LINKED", "SUCCESS", actor=actor, reason=detail,
          document_hash=po["document_hash"], details={
              "po_id": po_id, "po_number": po["po_number"], "vendor_id": vendor_id,
              "vendor_name": vendor["company_name"], "match_method": method,
              "po_tax_id_present": bool(po_tax), "vendor_tax_id_present": bool(vendor_tax),
              "tax_id_conflict_overridden": method == "AUTHORIZED_IDENTITY_CONFLICT_OVERRIDE",
              "review_note": reason.strip(),
          })
    updated_po = conn.execute("SELECT * FROM purchase_orders WHERE id=?", (po_id,)).fetchone()
    reassessed = _reassess_related_invoices(conn, updated_po, actor=actor,
                                            reason="Risk assessment rerun after purchase-order vendor relationship was established.")
    return {"status": "MATCHED", "vendor_id": vendor_id, "method": method,
            "reassessed_invoices": reassessed}


def relink_unlinked_purchase_orders(conn, vendor_id: str, *, actor: dict | None = None) -> dict[str, Any]:
    """Idempotently relink unresolved POs to a newly verified vendor using tax ID only."""
    vendor = conn.execute("SELECT * FROM vendors WHERE id=?", (vendor_id,)).fetchone()
    if not vendor or vendor["verification_status"] != "VERIFIED":
        return {"linked_po_ids": [], "candidate_po_ids": [], "conflict_po_ids": []}

    rows = conn.execute("SELECT * FROM purchase_orders WHERE vendor_id IS NULL ORDER BY created_at, id").fetchall()
    linked: list[str] = []
    candidates: list[str] = []
    conflicts: list[str] = []
    vendor_tax = normalize_tax_id(vendor["tax_id"])
    vendor_name = normalize_legal_name(vendor["company_name"])

    for po in rows:
        po_tax = normalize_tax_id(po["vendor_tax_id"])
        po_name = normalize_legal_name(po["vendor_company_name"])
        if po_tax and vendor_tax and po_tax == vendor_tax:
            # Tax ID is the strong identifier. The risk engine will separately flag
            # conflicting printed names/financial details rather than suppressing them.
            result = link_po_to_vendor(conn, po_id=po["id"], vendor_id=vendor_id, actor=actor,
                                       reason="Automatic relink after vendor approval based on exact normalized tax ID.")
            linked.append(po["id"])
            continue
        if po_tax and vendor_tax and po_tax != vendor_tax and po_name and po_name == vendor_name:
            reason = "Supplier name matched a verified vendor, but reliable tax IDs conflict. Manual identity review is required."
            _write_po_match(conn, po["id"], status="IDENTITY_CONFLICT", method="NAME_MATCH_TAX_ID_CONFLICT", reason=reason)
            audit(conn, "PO_VENDOR_IDENTITY_CONFLICT", "REVIEW_REQUIRED", actor=actor,
                  document_hash=po["document_hash"], reason=reason,
                  details={"po_id": po["id"], "po_number": po["po_number"], "vendor_id": vendor_id,
                           "vendor_name": vendor["company_name"], "tax_id_values_logged": False})
            conflicts.append(po["id"])
            continue
        if po_name and po_name == vendor_name:
            reason = "Exact supplier name candidate found after vendor approval, but no exact tax-ID match is available. Authorized identity confirmation is required."
            _write_po_match(conn, po["id"], status="CANDIDATE", method="EXACT_NAME_CANDIDATE", reason=reason)
            audit(conn, "PO_VENDOR_MATCH_CANDIDATE", "REVIEW_REQUIRED", actor=actor,
                  document_hash=po["document_hash"], reason=reason,
                  details={"po_id": po["id"], "po_number": po["po_number"], "vendor_id": vendor_id,
                           "match_method": "EXACT_NAME_CANDIDATE", "trusted_link_created": False})
            candidates.append(po["id"])
    return {"linked_po_ids": linked, "candidate_po_ids": candidates, "conflict_po_ids": conflicts}


def get_po_vendor_candidates(conn, po_id: str) -> list[dict[str, Any]]:
    po = conn.execute("SELECT * FROM purchase_orders WHERE id=?", (po_id,)).fetchone()
    if not po:
        return []
    result = []
    po_tax = normalize_tax_id(po["vendor_tax_id"])
    po_name = normalize_legal_name(po["vendor_company_name"])
    for vendor in _vendor_rows(conn):
        vendor_tax = normalize_tax_id(vendor["tax_id"])
        vendor_name = normalize_legal_name(vendor["company_name"])
        exact_tax = bool(po_tax and vendor_tax and po_tax == vendor_tax)
        exact_name = bool(po_name and vendor_name and po_name == vendor_name)
        if not (exact_tax or exact_name):
            continue
        item = dict(vendor)
        item["exact_tax_id_match"] = exact_tax
        item["exact_name_match"] = exact_name
        item["tax_id_conflict"] = bool(po_tax and vendor_tax and po_tax != vendor_tax)
        result.append(item)
    result.sort(key=lambda x: (not x["exact_tax_id_match"], not x["exact_name_match"], x["company_name"].casefold()))
    return result
