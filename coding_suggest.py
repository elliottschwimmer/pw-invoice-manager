"""Suggests which PO budget line(s) an invoice's charges probably belong
to, so a PM doesn't have to manually cross-reference every line by hand.

Two signals, combined:
  1. Remaining budget — how much of each PO line is left after what's
     already been coded to it on other invoices. A line with $0 left is
     never a plausible match.
  2. Description similarity — word overlap between the invoice's own
     itemized line descriptions (goods/services/warranty/whatever the
     vendor printed) and each PO line's description.

This is deliberately simple word-overlap matching, not AI — good enough to
narrow down the obvious cases; anything ambiguous still needs a human to
pick. Every suggestion is editable/overridable after being applied, same
as manually-entered coding.
"""
from __future__ import annotations

import re
from decimal import Decimal

from models import Invoice, InvoiceCodingLine, POBudgetLine
from parser import parse_invoice_line_items

_STOPWORDS = {
    "the", "and", "for", "of", "to", "a", "an", "in", "on", "with", "at",
    "or", "per", "each", "svcs", "services", "svc", "misc", "miscellaneous",
}


def _words(text: str) -> set:
    if not text:
        return set()
    tokens = re.findall(r"[a-z]{3,}", text.lower())
    return {t for t in tokens if t not in _STOPWORDS}


def remaining_budget(po_line: POBudgetLine, exclude_invoice_id: int = None) -> Decimal:
    """How much of this PO line hasn't already been coded to another
    invoice. Matches by (purchase_order, line_number) since that's what's
    preserved when a PO's lines are copied onto an invoice."""
    used = Decimal("0")
    invoices = Invoice.query.filter_by(purchase_order_id=po_line.purchase_order_id).all()
    for invoice in invoices:
        if invoice.id == exclude_invoice_id:
            continue
        for coding_line in invoice.coding_lines:
            if coding_line.line_number == po_line.line_number:
                used += coding_line.amount or 0
    budgeted = po_line.budgeted_amount or Decimal("0")
    return budgeted - used


def suggest_coding_lines(invoice: Invoice) -> list[dict]:
    """Returns suggested coding lines: [{line_number, account_string,
    description, amount, matched_item, confidence}]. Empty list if the
    invoice isn't linked to a PO, or nothing plausible was found."""
    if not invoice.purchase_order:
        return []

    po_lines = invoice.purchase_order.budget_lines
    if not po_lines:
        return []

    remaining = {pl.id: remaining_budget(pl, exclude_invoice_id=invoice.id) for pl in po_lines}
    items = parse_invoice_line_items(invoice.extracted_text or "")

    if not items:
        # No itemized breakdown found — fall back to suggesting a single
        # PO line for the whole invoice amount, only if exactly one line
        # has enough remaining budget to plausibly cover it (otherwise
        # it's a genuine judgment call, so leave it to the PM).
        if invoice.amount is None:
            return []
        candidates = [pl for pl in po_lines if remaining[pl.id] >= invoice.amount - Decimal("0.01")]
        if len(candidates) != 1:
            return []
        pl = candidates[0]
        return [{
            "line_number": pl.line_number, "account_string": pl.account_string,
            "description": pl.description, "amount": invoice.amount,
            "matched_item": "(whole invoice — only one PO line had enough budget)",
            "confidence": "low",
        }]

    suggestions = {}  # po_line.id -> suggestion dict (merged if multiple items match the same line)
    for item in items:
        item_words = _words(item["description"])
        best_pl, best_score = None, 0.0
        for pl in po_lines:
            if remaining[pl.id] < item["amount"] - Decimal("0.01"):
                continue  # not enough budget left on this line for this item
            pl_words = _words(pl.description)
            if not item_words or not pl_words:
                continue
            overlap = len(item_words & pl_words)
            score = overlap / len(item_words | pl_words)
            if score > best_score:
                best_pl, best_score = pl, score

        if best_pl is None or best_score < 0.15:
            continue  # no plausible match for this item — leave it to the PM

        remaining[best_pl.id] -= item["amount"]  # reserve so later items don't double-spend it
        key = best_pl.id
        if key in suggestions:
            suggestions[key]["amount"] += item["amount"]
            suggestions[key]["matched_item"] += f"; {item['description']}"
        else:
            suggestions[key] = {
                "line_number": best_pl.line_number,
                "account_string": best_pl.account_string,
                "description": best_pl.description,
                "amount": item["amount"],
                "matched_item": item["description"],
                "confidence": "high" if best_score >= 0.4 else "medium",
            }

    _reconcile_to_invoice_total(suggestions, invoice.amount)
    return sorted(suggestions.values(), key=lambda s: s["line_number"] or 0)


def _reconcile_to_invoice_total(suggestions: dict, invoice_amount):
    """Item-level matching often won't add up to the invoice's total on
    its own — tax, shipping, or rounding differences rarely show up as
    their own itemized line. Rather than leaving that difference
    unallocated, spread it proportionally across the matched lines so the
    suggested coding always sums to exactly what's owed."""
    if invoice_amount is None or not suggestions:
        return
    total_matched = sum(s["amount"] for s in suggestions.values())
    if total_matched <= 0 or total_matched == invoice_amount:
        return

    scale = invoice_amount / total_matched
    lines = list(suggestions.values())
    running = Decimal("0")
    for i, s in enumerate(lines):
        if i == len(lines) - 1:
            s["amount"] = invoice_amount - running  # absorb rounding on the last line
        else:
            adjusted = (s["amount"] * scale).quantize(Decimal("0.01"))
            s["amount"] = adjusted
            running += adjusted
