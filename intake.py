"""Turns a raw inbound email+PDF into an Invoice row: extract fields, match
vendor/PO, auto-route to PM/Administrator if we've seen this vendor before,
and fire the vendor + PM notification emails."""
from __future__ import annotations

from datetime import datetime, timedelta

from flask import current_app, render_template

from models import (
    db, Invoice, InvoiceCodingLine, InvoiceEvent, Vendor, VendorAssignment,
    PurchaseOrder,
)
from parser import extract_text, parse_invoice_fields, split_invoices
from email_service import get_email_backend
from pdf_export import generate_final_pdf

BERKELEY_DOMAIN = "berkeleyca.gov"


def calculate_due_date(fields: dict, received_at: datetime):
    """Priority: an explicit due date printed on the invoice, else the
    invoice's own printed date + its Net 30/60 terms, else — only when no
    date could be found on the document at all — the date it arrived in
    the inbox + Net terms, as a last resort."""
    if fields.get("due_date") is not None:
        return fields["due_date"]

    net_days = fields.get("net_terms_days")
    if net_days is None:
        return None

    base_date = fields.get("invoice_date") or received_at.date()
    return base_date + timedelta(days=net_days)


def ingest_new_invoices():
    backend = get_email_backend()
    created = []
    for msg in backend.fetch_new_messages():
        created.extend(_ingest_one_pdf(msg))
    return created


def ingest_one_message(msg: dict) -> list[Invoice]:
    """Same per-message ingestion the mailbox poller uses, exposed for
    other intake paths that already have one message's data in hand
    rather than a whole inbox to poll — e.g. the Power Automate webhook,
    an interim workaround for automatic ingestion while the Graph API app
    registration is pending IT approval. `msg` needs at least `data`
    (PDF bytes) and `filename`; `sender_email`, `subject`, `cc_emails`,
    and `message_id` are optional context."""
    return _ingest_one_pdf(msg)


def create_invoice_from_upload(data: bytes, filename: str, uploaded_by: str = "") -> list[Invoice]:
    """Manual upload path — no mailbox involved. Used for solo use before
    the shared inbox is connected: drop a PDF straight into the app."""
    msg = {
        "data": data,
        "filename": filename,
        "sender_email": None,
        "subject": f"Manually uploaded by {uploaded_by}" if uploaded_by else "Manually uploaded",
        "cc_emails": "",
        "message_id": None,
    }
    return _ingest_one_pdf(msg)


def _ingest_one_pdf(msg: dict) -> list[Invoice]:
    """Shared by the mock-inbox poller and manual upload: splits a
    multi-invoice PDF into one Invoice record per detected invoice (if
    applicable), otherwise creates a single Invoice from the whole file."""
    created = []
    split_parts = split_invoices(msg["data"])
    if not split_parts:
        created.append(_create_invoice_from_message(msg))
        return created

    first_invoice = None
    for part in split_parts:
        part_msg = dict(msg, data=part["data"], filename=f"{msg['filename']} (split)")
        invoice = _create_invoice_from_message(part_msg, precomputed_text=part["text"])
        if first_invoice is None:
            first_invoice = invoice
        else:
            invoice.split_from_invoice_id = first_invoice.id
            _log_event(invoice, "split_from_pdf", f"Split from the same PDF as invoice #{first_invoice.id} — {len(split_parts)} invoices detected in one file")
            db.session.commit()
        created.append(invoice)
    return created


def update_coding_lines(invoice: Invoice, lines: list[dict]):
    """Replaces an invoice's budget coding with what the PM/Administrator
    entered directly in the dashboard. `lines` is a list of dicts with
    line_number, account_string, description, amount."""
    InvoiceCodingLine.query.filter_by(invoice_id=invoice.id).delete()
    db.session.flush()
    for line in lines:
        db.session.add(
            InvoiceCodingLine(
                invoice_id=invoice.id,
                line_number=line.get("line_number"),
                account_string=line["account_string"],
                description=line.get("description", ""),
                amount=line.get("amount") or 0,
                source="pm_edit",
            )
        )
        # One insert at a time rather than one batched multi-row statement —
        # sidesteps a SQLAlchemy/psycopg2 "insertmanyvalues" mismatch seen
        # in production where a later row's amount landed in an earlier
        # row's line_number column, corrupting the whole batch insert.
        db.session.flush()
    _log_event(invoice, "coding_updated", f"Budget coding updated in the dashboard ({len(lines)} line(s))")
    db.session.commit()


def _create_invoice_from_message(msg: dict, precomputed_text: str = None) -> Invoice:
    text = precomputed_text if precomputed_text is not None else extract_text(msg["data"])
    fields = parse_invoice_fields(text)

    sender_email = (msg.get("sender_email") or "").lower()
    domain_verified = sender_email.endswith("@" + BERKELEY_DOMAIN)

    vendor = _match_or_create_vendor(fields.get("vendor_name_guess"), sender_email)

    received_at = datetime.utcnow()
    due_date = calculate_due_date(fields, received_at)

    invoice = Invoice(
        vendor=vendor,
        vendor_name_raw=fields.get("vendor_name_guess"),
        invoice_number=fields.get("invoice_number"),
        amount=fields.get("amount"),
        po_number=fields.get("po_number"),
        due_date=due_date,
        received_at=received_at,
        sender_email=msg.get("sender_email"),
        sender_domain_verified=domain_verified,
        cc_emails=msg.get("cc_emails"),
        email_subject=msg.get("subject"),
        email_message_id=msg.get("message_id"),
        filename=msg["filename"],
        data=msg["data"],
        mimetype="application/pdf",
        extracted_text=text,
        status="needs_assignment",
    )
    db.session.add(invoice)
    db.session.flush()
    _log_event(invoice, "received", f"Received from {msg.get('sender_email')}")

    # Try to match a PO on file for pre-filled budget coding.
    if fields.get("po_number"):
        po = PurchaseOrder.query.filter_by(po_number=fields["po_number"]).first()
        if po:
            _apply_po_to_invoice(invoice, po, log=False)
            _log_event(invoice, "po_matched", f"Matched PO {po.po_number}, pre-filled {len(po.budget_lines)} line(s)")

    # Auto-route if we've seen this vendor before.
    assignment = None
    if vendor:
        assignment = VendorAssignment.query.filter_by(vendor_id=vendor.id).first()
    if assignment:
        invoice.pm_id = assignment.pm_id
        invoice.administrator_id = assignment.administrator_id
        invoice.status = "pending_pm_approval"
        _log_event(invoice, "auto_assigned", f"Auto-assigned to PM {assignment.pm.name} / Admin {assignment.administrator.name}")

    db.session.commit()

    _send_vendor_ack(invoice)
    if invoice.status == "pending_pm_approval":
        _send_pm_notification(invoice)

    return invoice


def _match_or_create_vendor(name_guess, sender_email) -> Vendor | None:
    name_guess = (name_guess or "").strip()
    domain = sender_email.split("@", 1)[1] if sender_email and "@" in sender_email else None
    if not name_guess and not domain:
        return None

    vendor = None
    if name_guess:
        # Exact (case-insensitive) name match only — never let a shared
        # sender domain silently merge two different companies together.
        vendor = Vendor.query.filter(db.func.lower(Vendor.name) == name_guess.lower()).first()
    elif domain:
        # No name detected on the invoice at all — domain is the only
        # signal we have, so use it as a last resort.
        vendor = Vendor.query.filter_by(email_domain=domain).first()

    if not vendor:
        vendor = Vendor(name=name_guess or domain or "Unknown Vendor", email_domain=domain)
        db.session.add(vendor)
        db.session.flush()
    return vendor


def _apply_po_to_invoice(invoice: Invoice, po: PurchaseOrder, log=True):
    """Links an invoice to a PO and copies its budget lines over (carrying
    line numbers), replacing whatever coding was there before. Used both
    for auto-matching by PO number and manual PM selection.

    Amount starts at $0 for every line, not the line's full budgeted
    amount — a PO line's total budget is usually spread across many
    invoices over time, so defaulting every line to its full amount the
    moment a PO is linked is never actually right for this one invoice.
    "Suggest lines from PO" (or the PM by hand) fills in the real amounts."""
    invoice.purchase_order = po
    invoice.po_number = po.po_number
    InvoiceCodingLine.query.filter_by(invoice_id=invoice.id).delete()
    db.session.flush()
    for line in po.budget_lines:
        db.session.add(
            InvoiceCodingLine(
                invoice_id=invoice.id,
                line_number=line.line_number,
                account_string=line.account_string,
                description=line.description,
                amount=0,
                source="po",
            )
        )
        db.session.flush()  # one insert at a time — see update_coding_lines
    if log:
        _log_event(invoice, "po_linked", f"Linked to PO {po.po_number} — {len(po.budget_lines)} budget line(s) copied in")
        db.session.commit()


def link_purchase_order(invoice: Invoice, po_id: int):
    """Lets a PM manually attach an invoice to an existing PO when the
    invoice itself doesn't list a PO number (or the auto-match missed)."""
    po = PurchaseOrder.query.get(po_id)
    if not po:
        return
    _apply_po_to_invoice(invoice, po)


def correct_vendor(invoice: Invoice, vendor_name: str):
    """Lets a staff member fix a wrong/missing vendor match right on the
    invoice page. Reuses an existing vendor by exact name if one exists,
    otherwise creates it — same rule as auto-ingest, just human-triggered."""
    vendor_name = (vendor_name or "").strip()
    if not vendor_name:
        return

    old_name = invoice.vendor.name if invoice.vendor else invoice.vendor_name_raw

    vendor = Vendor.query.filter(db.func.lower(Vendor.name) == vendor_name.lower()).first()
    if not vendor:
        vendor = Vendor(name=vendor_name, email_domain=invoice.vendor.email_domain if invoice.vendor else None)
        db.session.add(vendor)
        db.session.flush()

    invoice.vendor = vendor
    invoice.vendor_name_raw = vendor_name
    _log_event(invoice, "vendor_corrected", f"Vendor corrected from '{old_name}' to '{vendor.name}'")
    db.session.commit()


def assign_invoice(invoice: Invoice, pm, administrator, remember=True):
    invoice.pm_id = pm.id
    invoice.administrator_id = administrator.id
    invoice.status = "pending_pm_approval"
    _log_event(invoice, "assigned", f"Assigned to PM {pm.name} / Admin {administrator.name}")

    if remember and invoice.vendor_id:
        existing = VendorAssignment.query.filter_by(vendor_id=invoice.vendor_id).first()
        if not existing:
            db.session.add(
                VendorAssignment(
                    vendor_id=invoice.vendor_id, pm_id=pm.id, administrator_id=administrator.id
                )
            )
    db.session.commit()
    _send_pm_notification(invoice)


def approve_invoice(invoice: Invoice, note: str = ""):
    invoice.status = "approved"
    invoice.pm_approved_at = datetime.utcnow()
    invoice.pm_approval_note = note
    _log_event(invoice, "approved", note or "Approved by PM")
    db.session.commit()
    _send_admin_ready_notification(invoice)


def unapprove_invoice(invoice: Invoice, note: str = ""):
    """Pulls an approved invoice back to Pending PM Approval so its budget
    coding can be corrected — most commonly because a PO line turned out
    not to have enough budget left to actually cover it. The coding lines
    themselves aren't touched; only the approval is undone."""
    invoice.status = "pending_pm_approval"
    invoice.pm_approved_at = None
    invoice.pm_approval_note = None
    _log_event(invoice, "unapproved", note or "Pulled back from Approved for corrections")
    db.session.commit()


def mark_entered_in_munis(invoice: Invoice, entered_by: str):
    invoice.status = "entered_in_munis"
    invoice.munis_entered_at = datetime.utcnow()
    invoice.munis_entered_by = entered_by
    _log_event(invoice, "entered_in_munis", f"Marked entered by {entered_by}")
    db.session.commit()


def send_pm_reminder(invoice: Invoice):
    _send_pm_notification(invoice, is_reminder=True)
    invoice.last_reminder_sent_at = datetime.utcnow()
    _log_event(invoice, "reminder_sent", f"Reminder sent to {invoice.pm.name if invoice.pm else 'PM'}")
    db.session.commit()


def _send_vendor_ack(invoice: Invoice):
    if not invoice.sender_domain_verified:
        return  # only auto-reply to berkeleyca.gov senders (AP forwarding)
    backend = get_email_backend()
    body = render_template("emails/vendor_ack.txt", invoice=invoice)
    backend.send_email(
        to_email=invoice.sender_email,
        subject=f"Re: {invoice.email_subject or 'Invoice received'}",
        body=body,
        invoice_id=invoice.id,
        in_reply_to=invoice.email_message_id,
    )


def _invoice_url(invoice: Invoice) -> str:
    base = current_app.config.get("BASE_URL", "").rstrip("/")
    return f"{base}/invoices/{invoice.id}"


def _send_pm_notification(invoice: Invoice, is_reminder=False):
    if not invoice.pm:
        return
    backend = get_email_backend()
    subject = ("REMINDER: " if is_reminder else "") + f"Invoice {invoice.invoice_number or ''} from {invoice.vendor.name if invoice.vendor else invoice.vendor_name_raw} — approval needed"
    body = render_template(
        "emails/pm_notification.txt", invoice=invoice, is_reminder=is_reminder,
        dashboard_url=_invoice_url(invoice),
    )
    backend.send_email(to_email=invoice.pm.email, subject=subject, body=body, invoice_id=invoice.id)


def _send_admin_ready_notification(invoice: Invoice):
    if not invoice.administrator:
        return
    backend = get_email_backend()
    subject = f"Invoice {invoice.invoice_number or ''} approved — ready for Munis entry"
    body = render_template(
        "emails/admin_notification.txt", invoice=invoice, dashboard_url=_invoice_url(invoice),
    )
    vendor_name = (invoice.vendor.name if invoice.vendor else invoice.vendor_name_raw or "invoice").replace(" ", "_")
    attachment = {
        "filename": f"{vendor_name}_{invoice.invoice_number or invoice.id}_approved.pdf",
        "data": generate_final_pdf(invoice),
        "mimetype": "application/pdf",
    }
    backend.send_email(
        to_email=invoice.administrator.email, subject=subject, body=body,
        invoice_id=invoice.id, attachment=attachment,
    )


def _log_event(invoice: Invoice, event_type: str, detail: str):
    db.session.add(InvoiceEvent(invoice=invoice, event_type=event_type, detail=detail))
