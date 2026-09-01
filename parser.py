"""Best-effort field extraction from invoice/PO PDFs.

This is deliberately simple regex-based extraction, not OCR/ML. It gets you
90% of the way for typewritten vendor invoices; anything it misses the PM or
Administrator can fix by hand in the dashboard. If invoices are scanned
images rather than text PDFs, we'll need to add OCR (pytesseract) later.
"""
from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal, InvalidOperation

import pdfplumber


def extract_text(pdf_bytes: bytes) -> str:
    return "\n".join(extract_pages_text(pdf_bytes))


def extract_pages_text(pdf_bytes: bytes) -> list[str]:
    """Same as extract_text but keeps each page's text separate — needed to
    detect where one invoice ends and another begins in a multi-invoice
    PDF (see split_invoices in intake.py)."""
    with pdfplumber.open(pdf_bytes if hasattr(pdf_bytes, "read") else _bytesio(pdf_bytes)) as pdf:
        return [page.extract_text() or "" for page in pdf.pages]


def _bytesio(data: bytes):
    import io
    return io.BytesIO(data)


# All label-based patterns below use [ \t]* (not \s*) between the label and
# its value, and are matched one line at a time — never with .search() on
# the whole document — so a label on one line can never pick up a value
# printed on the following line (e.g. "INVOICE" on its own line, followed
# by "Invoice #: 282644" below it).
_INVOICE_NUM_RE = re.compile(
    r"inv(?:oice)?\b\.?[ \t]*(?:#|no\.?|number)?[ \t]*#?[ \t]*:?[ \t]*([A-Za-z0-9][A-Za-z0-9\-]{2,31})",
    re.IGNORECASE,
)
# Column headers that can get mistaken for the invoice number itself when a
# vendor's invoice number is unlabeled and only identifiable by its position
# in a table (see _extract_invoice_number_from_table below).
_INVOICE_NUM_BLOCKLIST = {
    "date", "total", "due", "terms", "enclosed", "no", "number", "name",
    "page", "amount", "balance", "qty", "quantity", "description", "tax",
}
# A table-style invoice ("INVOICE #  DATE  TOTAL DUE  TERMS" as a header row,
# with the actual values below it) needs its own pass: the invoice number
# has no inline label, only a column position.
_TABLE_HEADER_RE = re.compile(r"invoice[ \t]*#.*\bdate\b", re.IGNORECASE)
# Negative lookahead for "box" so "P.O. Box 123, Berkeley CA" (a mailing
# address) doesn't get mistaken for a PO number. City PO numbers are 8 digits.
_PO_NUM_RE = re.compile(
    r"(?:P\.?O\.?|purchase[ \t]*order)[ \t]*(?!box)(?:#|no\.?|number)?[ \t]*[:#]?[ \t]*(\d{8})",
    re.IGNORECASE,
)
_AMOUNT_RE = re.compile(
    # \b before the label so "total" doesn't match inside "Subtotal" and
    # grab a subtotal instead of the real total.
    r"\b(?:total[ \t]*(?:due|amount)?|amount[ \t]*due|balance[ \t]*due|net[ \t]*amount)\b"
    # Up to 40 non-digit characters between the label and the number —
    # covers "Total USD________________10,861.34" (a currency code plus a
    # run of underscores used as a remittance-slip fill line), not just a
    # bare "$" or colon.
    r"[^\d\n]{0,40}([\d,]+\.\d{2})",
    re.IGNORECASE,
)
_FALLBACK_AMOUNT_RE = re.compile(r"\$[ \t]*([\d,]+\.\d{2})")
# Shared date-value pattern: either numeric (08/20/2026) or written-out
# (August 20, 2026).
_DATE_VALUE = (
    r"(\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4}|"
    r"(?:January|February|March|April|May|June|July|August|September|October|"
    r"November|December)\s+\d{1,2},?\s+\d{4})"
)
_DUE_DATE_RE = re.compile(
    r"(?:due[ \t]*date|payment[ \t]*due|net[ \t]*due|due[ \t]*by)[ \t]*[:\-]?[ \t]*" + _DATE_VALUE,
    re.IGNORECASE,
)
# Explicit invoice date, e.g. "Invoice Date: 08/20/2026" or "Date: 08/11/26".
# Used with net_terms_days to compute a due date — preferred over falling
# back to whenever the invoice happened to reach the inbox.
_INVOICE_DATE_RE = re.compile(
    r"(?:invoice[ \t]*date|^date)[ \t]*[:\-]?[ \t]*" + _DATE_VALUE,
    re.IGNORECASE,
)
# "Net 30" / "Net 60" style payment terms — combined with the invoice date
# (or, failing that, the date received) as a due-date fallback.
_NET_TERMS_RE = re.compile(r"\bnet[ \t]*(\d{1,3})\b", re.IGNORECASE)
_DATE_TOKEN_RE = re.compile(r"^\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4}$")
# Written-out dates like "July 2, 2026" — some invoices (e.g. LAZ Parking)
# use this instead of mm/dd/yyyy.
_MONTH_DATE_RE = re.compile(
    r"\b(January|February|March|April|May|June|July|August|September|October|"
    r"November|December)\s+\d{1,2},?\s+\d{4}\b",
    re.IGNORECASE,
)
# A table-style invoice with a "Due Date" column but no inline label next to
# the actual date value — the header row names the columns, the date sits
# in the data row below it (see LAZ Parking: "Document Date ... Due Date
# Terms" header, "July 2, 2026 August 1, 2026 Net 30" data row).
_DUE_DATE_HEADER_RE = re.compile(r"\bdue[ \t]*date\b", re.IGNORECASE)

# A "From:" line is the strongest vendor-name signal when present (common on
# purchase-order-style invoice layouts). Stops at "Remit To" if the same
# line repeats the name a second time after that label.
_VENDOR_FROM_RE = re.compile(
    r"^from[ \t]*:?[ \t]*(.+?)(?:[ \t]+remit[ \t]*to[ \t]*:.*)?$", re.IGNORECASE
)

# Header lines that are common on invoices but are never the vendor's name.
_VENDOR_LINE_SKIP_RE = re.compile(
    r"^(invoice|bill\s*to|remit\s*to|ship\s*to|sold\s*to|page\s*\d|p\.?o\.?\s*box"
    r"|statement|date|account\s*(#|no|number)|customer\s*(#|no|number)"
    r"|total|amount|balance|due\b)",
    re.IGNORECASE,
)


def parse_invoice_fields(text: str) -> dict:
    fields = {
        "invoice_number": None,
        "po_number": None,
        "amount": None,
        "due_date": None,
        "invoice_date": None,
        "net_terms_days": None,
        "vendor_name_guess": None,
    }

    lines = text.splitlines()

    for line in lines:
        if fields["invoice_number"] is None:
            m = _INVOICE_NUM_RE.search(line)
            if m:
                value = m.group(1)
                # A bare "Invoice"/"Inv" with no "#"/":" marker (e.g. inside
                # a company name like "Multi Invoice Vendor") can only be
                # trusted as a real label if the captured value has a digit
                # in it — otherwise it's just grabbing the next plain word.
                has_marker = "#" in m.group(0) or ":" in m.group(0)
                looks_like_number = has_marker or any(ch.isdigit() for ch in value)
                if looks_like_number and value.lower() not in _INVOICE_NUM_BLOCKLIST:
                    fields["invoice_number"] = value.strip()
        if fields["po_number"] is None:
            m = _PO_NUM_RE.search(line)
            if m:
                fields["po_number"] = m.group(1).strip()
        if fields["due_date"] is None:
            m = _DUE_DATE_RE.search(line)
            if m:
                fields["due_date"] = _to_date(m.group(1))
        if fields["invoice_date"] is None:
            m = _INVOICE_DATE_RE.search(line)
            if m:
                fields["invoice_date"] = _to_date(m.group(1))
        if fields["net_terms_days"] is None:
            m = _NET_TERMS_RE.search(line)
            if m:
                fields["net_terms_days"] = int(m.group(1))

    if fields["invoice_number"] is None:
        fields["invoice_number"] = _extract_invoice_number_from_table(lines)

    if fields["invoice_date"] is None:
        fields["invoice_date"] = _extract_invoice_date_from_table(lines)

    if fields["due_date"] is None:
        fields["due_date"] = _extract_due_date_from_column(lines)

    m = _AMOUNT_RE.search(text)
    if not m:
        # fall back to the largest dollar figure on the page — usually the total
        amounts = _FALLBACK_AMOUNT_RE.findall(text)
        if amounts:
            m_val = max(amounts, key=lambda a: _to_decimal(a))
            fields["amount"] = _to_decimal(m_val)
    else:
        fields["amount"] = _to_decimal(m.group(1))

    # Vendor name guess: prefer an explicit "From:" line (strongest signal),
    # else fall back to the first non-empty line that isn't a generic
    # invoice header (rough, but works for most letterhead invoices).
    for line in lines:
        line = line.strip()
        if not line:
            continue
        m = _VENDOR_FROM_RE.match(line)
        if m and m.group(1).strip():
            fields["vendor_name_guess"] = m.group(1).strip()
            break

    if not fields["vendor_name_guess"]:
        for line in lines:
            line = line.strip()
            if (
                line and len(line) < 80 and "$" not in line
                and not _VENDOR_LINE_SKIP_RE.match(line)
                and not _ANY_DATE_RE.fullmatch(line)
            ):
                fields["vendor_name_guess"] = line
                break

    return fields


def _extract_invoice_number_from_table(lines: list[str]):
    """Handles layouts like:
        INVOICE #   DATE         TOTAL DUE    TERMS   ENCLOSED
        5015        02/15/2026   $1,309.12    Net 30
    where the invoice number has no inline label and is only identifiable
    by being the first column of the row right after the header row."""
    for i, line in enumerate(lines):
        if not _TABLE_HEADER_RE.search(line):
            continue
        for data_line in lines[i + 1: i + 3]:
            data_line = data_line.strip()
            if not data_line:
                continue
            first_token = data_line.split()[0]
            if re.match(r"^[A-Za-z0-9\-]{2,32}$", first_token) and first_token.lower() not in _INVOICE_NUM_BLOCKLIST:
                return first_token
            break
    return None


def _extract_invoice_date_from_table(lines: list[str]):
    """Same table-header situation as _extract_invoice_number_from_table:
        INVOICE #   DATE         TOTAL DUE    TERMS   ENCLOSED
        5015        02/15/2026   $1,309.12    Net 30
    the date has no inline label either, just a column position."""
    for i, line in enumerate(lines):
        if not _TABLE_HEADER_RE.search(line):
            continue
        for data_line in lines[i + 1: i + 3]:
            data_line = data_line.strip()
            if not data_line:
                continue
            for token in data_line.split():
                if _DATE_TOKEN_RE.match(token):
                    return _to_date(token)
            break
    return None


_ANY_DATE_RE = re.compile(_DATE_VALUE)


_DATE_WORD_RE = re.compile(r"\bdate\b", re.IGNORECASE)


def _extract_due_date_from_column(lines: list[str]):
    """Handles a "Due Date" column with no date directly on the same line —
    the date sits in a data row below a header row, at whatever position
    "Due Date" is among the OTHER date-named columns (Document Date, Ship
    Date, Invoice Date, etc; non-date columns like "Terms" or "PO #" don't
    produce a parseable date value, so they don't shift the count):

        LAZ Parking:
          "Document Date  Customer PO No.  Due Date  Terms"
          "July 2, 2026  August 1, 2026  Net 30"
          -> 1 date-column ("Document Date") before "Due Date" -> take the
             2nd date found (index 1) -> August 1, 2026

        IPS Group:
          "Quote No.  PO #  Terms  Due Date  Ship Via  Ship Date  Tracking #"
          "IPS-32026-0  Net 30  9/19/2026  8/20/2026"
          -> 0 date-columns before "Due Date" -> take the 1st date found
             (index 0) -> 9/19/2026, not the later Ship Date
    """
    for i, line in enumerate(lines):
        m = _DUE_DATE_HEADER_RE.search(line)
        if not m:
            continue
        date_columns_before = len(_DATE_WORD_RE.findall(line[:m.start()]))
        for data_line in lines[i + 1: i + 3]:
            data_line = data_line.strip()
            if not data_line:
                continue
            dates = _ANY_DATE_RE.findall(data_line)
            if dates:
                index = min(date_columns_before, len(dates) - 1)
                return _to_date(dates[index])
            break
    return None


def _to_decimal(s: str) -> Decimal:
    try:
        return Decimal(s.replace(",", ""))
    except InvalidOperation:
        return Decimal("0.00")


def _to_date(s: str):
    s = s.strip()
    numeric = s.replace("-", "/")
    for fmt in ("%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(numeric, fmt).date()
        except ValueError:
            continue
    for fmt in ("%B %d, %Y", "%B %d %Y", "%b %d, %Y", "%b %d %Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def split_invoices(pdf_bytes: bytes):
    """Detects whether a PDF contains more than one invoice (a vendor
    sometimes bundles several into a single attachment) and, if so, splits
    it into separate PDFs — one per invoice — each with its own page range.

    Heuristic: walk the pages in order; a page starts a NEW invoice only
    when it shows both a different invoice number AND its own dollar
    amount (requiring both signals cuts down on false splits from stray
    text that merely resembles an invoice number). Any page with no new
    number (or the same one, e.g. a repeated footer) is treated as a
    continuation of the current invoice.

    Returns None if the PDF looks like a single invoice (the common case —
    callers should fall back to treating it as one). Otherwise returns a
    list of {"data": bytes, "text": str} dicts, one per detected invoice.
    """
    from io import BytesIO
    from pypdf import PdfReader, PdfWriter

    pages_text = extract_pages_text(pdf_bytes)
    if len(pages_text) <= 1:
        return None

    groups = []
    current_number = None
    for i, text in enumerate(pages_text):
        fields = parse_invoice_fields(text)
        detected = fields.get("invoice_number")
        is_new_invoice = (
            detected and fields.get("amount") is not None and detected != current_number
        )
        if is_new_invoice or not groups:
            groups.append({"invoice_number": detected, "page_indices": [i]})
            current_number = detected or current_number
        else:
            groups[-1]["page_indices"].append(i)

    if len(groups) <= 1:
        return None

    reader = PdfReader(pdf_bytes if hasattr(pdf_bytes, "read") else BytesIO(pdf_bytes))
    results = []
    for group in groups:
        writer = PdfWriter()
        for idx in group["page_indices"]:
            writer.add_page(reader.pages[idx])
        out = BytesIO()
        writer.write(out)
        out.seek(0)
        results.append({
            "data": out.read(),
            "text": "\n".join(pages_text[idx] for idx in group["page_indices"]),
        })
    return results


# Rows like "Landscaping - Civic Center Park ... $3,000.00" — a description
# followed by a trailing dollar amount, generic enough to catch most vendor
# line-item tables. Used to power the PO-line coding suggestions.
_LINE_ITEM_RE = re.compile(r"^(.{4,80}?)\s+\$?\s*([\d,]+\.\d{2})\s*$")
_LINE_ITEM_SKIP_RE = re.compile(
    r"^(total|subtotal|balance|amount\s*due|net\s*amount|grand\s*total|"
    r"tax|sales\s*tax|shipping|freight|discount|"
    r"payments?|credits?|invoice|page\s*\d|bill\s*to|remit\s*to|ship\s*to)",
    re.IGNORECASE,
)
# Real PDF text extraction often merges unrelated columns onto one line
# (e.g. a phone number and "Subtotal USD" end up sharing a line), so these
# summary/total words are also checked anywhere in the line, not just at
# the start — a genuine line item essentially never contains them.
_LINE_ITEM_SKIP_ANYWHERE_RE = re.compile(
    r"\b(subtotal|sub\s*total|sales\s*tax|balance\s*due|net\s*amount|"
    r"grand\s*total|amount\s*due)\b",
    re.IGNORECASE,
)
# A merged-column line like "AR@ipsgroupinc.com Total" or "Pay Terms - Net
# 30 Days Total:" isn't caught by the start-anchored skip list, but its
# description always ENDS in one of these summary words right before the
# dollar amount — a real item description essentially never does.
_LINE_ITEM_DESC_SUFFIX_SKIP_RE = re.compile(
    r"(total|subtotal|sub\s*total|balance\s*due|net\s*amount|grand\s*total|"
    r"amount\s*due|sales\s*tax|\btax)\s*:?\s*$",
    re.IGNORECASE,
)


def parse_invoice_line_items(text: str) -> list[dict]:
    """Best-effort extraction of the invoice's own itemized lines (not the
    PO's), used to suggest which PO budget line each item probably belongs
    to. Skips obvious non-item rows (totals, tax, shipping, headers)."""
    items = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or _LINE_ITEM_SKIP_RE.match(line) or _LINE_ITEM_SKIP_ANYWHERE_RE.search(line):
            continue
        m = _LINE_ITEM_RE.match(line)
        if not m:
            continue
        if _LINE_ITEM_DESC_SUFFIX_SKIP_RE.search(m.group(1)):
            continue
        description, amount = m.groups()
        items.append({"description": description.strip(), "amount": _to_decimal(amount)})
    return items
