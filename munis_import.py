"""Parses a Tyler Munis "Purchase Order" export (an .xlsx with multiple
sheets: Purchase Order, Activity, Invoices, Audit, Receiving, and "PO Lines
and Line Details"). Munis exports are frequently malformed XLSX — openpyxl
chokes on the stylesheet — so this uses python-calamine, a much more
tolerant reader, instead.

Only the "Purchase Order" sheet (PO #, vendor, fiscal year, totals) and the
"PO Lines and Line Details" sheet (line #, description, unit price,
liquidation amount, balance) are used. There is no GL account string
anywhere in a Munis PO export — that's coded by the PM in this app, same
as always.
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from python_calamine import CalamineWorkbook


def parse_munis_po_export(file_bytes: bytes) -> dict:
    import io
    wb = CalamineWorkbook.from_filelike(io.BytesIO(file_bytes))

    po_header = _parse_po_sheet(wb)
    po_header["lines"] = _parse_lines_sheet(wb)
    return po_header


def _sheet_rows(wb, name: str) -> list[list]:
    if name not in wb.sheet_names:
        return []
    return wb.get_sheet_by_name(name).to_python()


def _parse_po_sheet(wb) -> dict:
    rows = _sheet_rows(wb, "Purchase Order")
    if len(rows) < 2:
        raise ValueError('No "Purchase Order" sheet found, or it has no data row — is this a Munis PO export?')

    header, data = rows[0], rows[1]
    row = dict(zip(header, data))

    return {
        "po_number": _clean_str(row.get("PO Number")),
        "fiscal_year": _to_int(row.get("Fiscal Year")),
        "description": _clean_str(row.get("Description")),
        "vendor_name": _clean_str(row.get("Vendor Name")),
        "munis_liquidated_total": _to_decimal(row.get("Liq. Amount")),
        "munis_open_amount": _to_decimal(row.get("Open Amount")),
    }


def _parse_lines_sheet(wb) -> list[dict]:
    rows = _sheet_rows(wb, "PO Lines and Line Details")
    if len(rows) < 2:
        return []

    header = rows[0]
    lines = []
    for data_row in rows[1:]:
        row = dict(zip(header, data_row))
        line_number = _to_int(row.get("Line"))
        if line_number is None:
            continue
        lines.append({
            "line_number": line_number,
            "description": _clean_str(row.get("Description")),
            "unit_price": _to_decimal(row.get("Unit Price")),
            "budgeted_amount": _to_decimal(row.get("Ordered Amount")),
            "munis_liquidated_amount": _to_decimal(row.get("Liquidation Amount")),
            "munis_balance": _to_decimal(row.get("Balance")),
        })
    return lines


def _clean_str(v):
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def _to_int(v):
    if v is None or v == "":
        return None
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _to_decimal(v):
    if v is None or v == "":
        return None
    try:
        return Decimal(str(v))
    except InvalidOperation:
        return None
