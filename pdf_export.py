"""Builds the final PDF an Administrator uploads to Tyler Munis: a coding/
approval cover page — vendor, amount, PM approval, and the budget-line
breakdown — merged in front of the original invoice PDF, so it's one file
that documents how and by whom the invoice was coded for payment."""
from io import BytesIO

from pypdf import PdfReader, PdfWriter
from reportlab.lib.pagesizes import letter
from reportlab.lib.units import inch
from reportlab.pdfgen import canvas

from timezone_utils import format_pacific


def _current_account_strings(invoice) -> dict:
    """Account string per line_number, resolved live from the linked PO's
    budget lines rather than trusting what's stored on the invoice's own
    coding lines — that copy is only made at link/save time, so if the PO's
    account string was entered or corrected afterward, the invoice would
    otherwise keep exporting the old (often blank) value forever."""
    if not invoice.purchase_order:
        return {}
    return {
        pl.line_number: pl.account_string
        for pl in invoice.purchase_order.budget_lines
        if pl.account_string
    }


def generate_final_pdf(invoice) -> bytes:
    cover = _build_cover_page(invoice)
    return _merge_pdfs(cover, invoice.data)


def generate_stamped_pdf(invoice) -> bytes:
    """Alternative to the cover-page version: stamps the coding/approval
    text directly onto the bottom margin of the invoice's last page,
    mimicking how it's written by hand today. Simpler and more familiar,
    but the fixed placement can run out of room on invoices with many
    budget lines, or overlap content on invoices with little margin —
    the cover-page version doesn't have either limitation."""
    reader = PdfReader(invoice.data if hasattr(invoice.data, "read") else BytesIO(invoice.data))
    last_page = reader.pages[-1]
    page_width = float(last_page.mediabox.width)
    page_height = float(last_page.mediabox.height)

    overlay_bytes = _build_stamp_overlay(invoice, page_width, page_height)
    overlay_reader = PdfReader(BytesIO(overlay_bytes))
    last_page.merge_page(overlay_reader.pages[0])

    writer = PdfWriter()
    for page in reader.pages:
        writer.add_page(page)

    out = BytesIO()
    writer.write(out)
    out.seek(0)
    return out.read()


_STAMP_RED = (200 / 255, 16 / 255, 46 / 255)  # Berkeley red, so the stamp stands out


def _build_stamp_overlay(invoice, page_width: float, page_height: float) -> bytes:
    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=(page_width, page_height))
    margin = 0.4 * inch
    line_gap = 9

    pm_name = invoice.pm.name if invoice.pm else "—"
    approved_at = format_pacific(invoice.pm_approved_at) if invoice.pm_approved_at else "—"

    # Coding lines, bottom-up (excludes the approval — that gets its own
    # signature block below, not just a plain text line).
    current_accounts = _current_account_strings(invoice)
    coded_lines = [cl for cl in invoice.coding_lines if cl.amount and float(cl.amount) > 0]
    coding_lines_bottom_up = []
    for coding_line in reversed(coded_lines):
        account_string = current_accounts.get(coding_line.line_number) or coding_line.account_string
        account = f" {account_string}" if account_string else ""
        coding_lines_bottom_up.append(
            f"  Line {coding_line.line_number or ''}:{account} — {_money(coding_line.amount)}"
        )
    if coded_lines:
        po_number = invoice.purchase_order.po_number if invoice.purchase_order else invoice.po_number
        po_part = f"PO {po_number} — " if po_number else ""
        coding_lines_bottom_up.append(f"{po_part}Budget Coding (Total {_money(invoice.coding_total)}):")

    # Signature block height: printed "Electronically approved..." line,
    # the signature underline, and the signature name above it.
    sig_block_height = 0
    if invoice.pm_approved_at:
        sig_block_height = line_gap + 6 + 15  # printed line + underline gap + name line

    y = margin + sig_block_height + line_gap * len(coding_lines_bottom_up)

    c.setStrokeColorRGB(0, 0, 0)
    c.line(margin, y + 10, page_width - margin, y + 10)

    def text_line(text, size=6.5, bold=False):
        nonlocal y
        c.setFont("Helvetica-Bold" if bold else "Helvetica", size)
        c.setFillColorRGB(*_STAMP_RED)
        c.drawString(margin, y, text)
        y -= line_gap

    for text in reversed(coding_lines_bottom_up):
        text_line(text, bold="Budget Coding" in text)

    if invoice.pm_approved_at:
        y -= 4
        sig_width = 1.8 * inch
        c.setFont("Helvetica-Oblique", 11)
        c.setFillColorRGB(*_STAMP_RED)
        c.drawString(margin, y, pm_name)
        y -= 6
        c.setStrokeColorRGB(*_STAMP_RED)
        c.line(margin, y, margin + sig_width, y)
        y -= 12
        text_line(f"Electronically approved for payment — {pm_name} — {approved_at}", bold=True)

    c.save()
    buf.seek(0)
    return buf.read()


def _build_cover_page(invoice) -> bytes:
    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    width, height = letter
    margin = 0.75 * inch
    y = height - margin

    def line(text, size=10, bold=False, gap=16):
        nonlocal y
        c.setFont("Helvetica-Bold" if bold else "Helvetica", size)
        c.drawString(margin, y, text)
        y -= gap

    line("City of Berkeley — Public Works", size=14, bold=True, gap=20)
    line("Invoice Approval & Budget Coding", size=12, bold=True, gap=24)

    vendor_name = invoice.vendor.name if invoice.vendor else invoice.vendor_name_raw or "Unknown Vendor"
    line(f"Vendor: {vendor_name}")
    line(f"Invoice Number: {invoice.invoice_number or '—'}")
    line(f"PO Number: {invoice.po_number or '—'}")
    line(f"Invoice Amount: {_money(invoice.amount)}")
    line(f"Due Date: {invoice.due_date.strftime('%m/%d/%Y') if invoice.due_date else '—'}")
    y -= 8

    line("Approval", size=11, bold=True, gap=18)
    pm_name = invoice.pm.name if invoice.pm else "—"
    approved_at = format_pacific(invoice.pm_approved_at) if invoice.pm_approved_at else "—"
    line(f"Approved by: {pm_name}")
    line(f"Approved on: {approved_at}")
    if invoice.pm_approval_note:
        line("Note:")
        for wrapped in _wrap(invoice.pm_approval_note, 95):
            line(f"  {wrapped}", size=9, gap=13)
    y -= 8

    line("Budget Line Coding", size=11, bold=True, gap=18)
    c.setFont("Helvetica-Bold", 9)
    c.drawString(margin, y, "Line")
    c.drawString(margin + 0.5 * inch, y, "Account String")
    c.drawString(margin + 6.0 * inch, y, "Amount")
    y -= 6
    c.line(margin, y, width - margin, y)
    y -= 14

    total = 0
    c.setFont("Helvetica", 9)
    current_accounts = _current_account_strings(invoice)
    coded_lines = [cl for cl in invoice.coding_lines if cl.amount and float(cl.amount) > 0]
    for coding_line in coded_lines:
        if y < margin + 40:
            c.showPage()
            y = height - margin
            c.setFont("Helvetica", 9)
        c.drawString(margin, y, str(coding_line.line_number or ""))
        account_string = current_accounts.get(coding_line.line_number) or coding_line.account_string
        account = (account_string or "")[:60]
        c.drawString(margin + 0.5 * inch, y, account)
        amt = float(coding_line.amount or 0)
        total += amt
        c.drawRightString(width - margin, y, _money(amt))
        y -= 14

    y -= 6
    c.line(margin, y, width - margin, y)
    y -= 16
    c.setFont("Helvetica-Bold", 10)
    c.drawString(margin, y, "Total Coded")
    c.drawRightString(width - margin, y, _money(total))
    y -= 40

    if invoice.pm_approved_at:
        if y < margin + 70:
            c.showPage()
            y = height - margin
        sig_width = 3.2 * inch
        c.setFont("Helvetica-Oblique", 16)
        c.drawString(margin, y, pm_name)
        y -= 4
        c.line(margin, y, margin + sig_width, y)
        y -= 14
        c.setFont("Helvetica", 8)
        c.drawString(margin, y, "Electronically approved for payment")
        y -= 12
        c.drawString(margin, y, f"{pm_name} — {approved_at}")
        y -= 30

    c.setFont("Helvetica-Oblique", 8)
    c.drawString(margin, margin / 2, "Generated by the Public Works Invoice Manager. The original invoice follows this page.")

    c.save()
    buf.seek(0)
    return buf.read()


def _merge_pdfs(cover_pdf_bytes: bytes, invoice_pdf_bytes: bytes) -> bytes:
    writer = PdfWriter()

    cover_reader = PdfReader(BytesIO(cover_pdf_bytes))
    for page in cover_reader.pages:
        writer.add_page(page)

    invoice_reader = PdfReader(BytesIO(invoice_pdf_bytes))
    for page in invoice_reader.pages:
        writer.add_page(page)

    out = BytesIO()
    writer.write(out)
    out.seek(0)
    return out.read()


def _money(value) -> str:
    if value is None:
        return "—"
    return "${:,.2f}".format(float(value))


def _wrap(text: str, width: int):
    words = text.split()
    lines, current = [], ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if len(candidate) > width:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines
