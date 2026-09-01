"""Turns a parsed Munis PO export into a PurchaseOrder record — creating it
if the PO number is new, or updating it in place if it already exists
(Munis exports get re-pulled periodically as liquidation amounts change).

On update, a PM's manually-entered account_string for each line is
preserved by matching on line_number — only the Munis-sourced fields
(description, unit price, liquidated amount, balance) get refreshed."""
from __future__ import annotations

from datetime import datetime

from models import db, PurchaseOrder, POBudgetLine, Vendor
from munis_import import parse_munis_po_export
from fiscal_year_utils import current_fiscal_year_label


def import_munis_po(file_bytes: bytes, uploaded_by: str = "") -> PurchaseOrder:
    parsed = parse_munis_po_export(file_bytes)
    if not parsed.get("po_number"):
        raise ValueError('Could not find a PO number in the "Purchase Order" sheet.')

    po = PurchaseOrder.query.filter_by(po_number=parsed["po_number"]).first()
    is_new = po is None
    if is_new:
        po = PurchaseOrder(po_number=parsed["po_number"], uploaded_by=uploaded_by)
        db.session.add(po)

    po.description = parsed.get("description") or po.description
    po.vendor_name = parsed.get("vendor_name") or po.vendor_name
    po.munis_liquidated_total = parsed.get("munis_liquidated_total")
    po.munis_open_amount = parsed.get("munis_open_amount")
    po.munis_imported_at = datetime.utcnow()

    if parsed.get("vendor_name"):
        po.vendor = _match_or_create_vendor(parsed["vendor_name"])

    if parsed.get("fiscal_year") and is_new:
        # Only set on first import — don't silently reclassify a PO a PM
        # already marked one-time vs fiscal-year.
        po.fiscal_year_scope = "fiscal_year"
        po.fiscal_year_label = f"FY{str(parsed['fiscal_year'])[-2:]}"
        # Leave fiscal_year_reviewed_at unset so the normal FY-review flag
        # applies if this PO is being carried into a new fiscal year.

    db.session.flush()

    existing_lines_by_number = {bl.line_number: bl for bl in po.budget_lines}
    imported_numbers = set()
    for line in parsed.get("lines", []):
        imported_numbers.add(line["line_number"])
        existing = existing_lines_by_number.get(line["line_number"])
        if existing:
            existing.description = line["description"]
            existing.unit_price = line["unit_price"]
            existing.budgeted_amount = line["budgeted_amount"]
            existing.munis_liquidated_amount = line["munis_liquidated_amount"]
            existing.munis_balance = line["munis_balance"]
            # account_string is left untouched — that's the PM's own coding.
        else:
            db.session.add(POBudgetLine(
                purchase_order_id=po.id,
                line_number=line["line_number"],
                account_string="",  # not present in a Munis export — PM codes this
                description=line["description"],
                unit_price=line["unit_price"],
                budgeted_amount=line["budgeted_amount"],
                munis_liquidated_amount=line["munis_liquidated_amount"],
                munis_balance=line["munis_balance"],
            ))
        db.session.flush()  # one insert at a time — see intake.update_coding_lines

    db.session.commit()
    return po


def _match_or_create_vendor(name: str) -> Vendor:
    vendor = Vendor.query.filter(db.func.lower(Vendor.name) == name.strip().lower()).first()
    if not vendor:
        vendor = Vendor(name=name.strip())
        db.session.add(vendor)
        db.session.flush()
    return vendor
