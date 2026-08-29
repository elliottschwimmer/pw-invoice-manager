from datetime import datetime

from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()


class Vendor(db.Model):
    __tablename__ = "vendors"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(256), nullable=False, index=True)
    email_domain = db.Column(db.String(128))

    invoices = db.relationship("Invoice", backref="vendor")
    purchase_orders = db.relationship("PurchaseOrder", backref="vendor")

    # Learned routing: once a human assigns a PM/Administrator for this
    # vendor, future invoices from the same vendor route automatically.
    assignments = db.relationship("VendorAssignment", backref="vendor")


class Staff(db.Model):
    """A project/program manager or administrator."""
    __tablename__ = "staff"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(128), nullable=False)
    email = db.Column(db.String(256), nullable=False, unique=True)
    role = db.Column(db.String(24), nullable=False)  # "pm" | "administrator"
    division = db.Column(db.String(128))  # e.g. "Transportation"
    sub_division = db.Column(db.String(128))  # e.g. "Parking Services"

    @property
    def team_label(self):
        if self.division and self.sub_division:
            return f"{self.division} — {self.sub_division}"
        return self.division or self.sub_division or ""


class VendorAssignment(db.Model):
    """Which PM and which Administrator handle a given vendor's invoices.
    Created manually the first time, then reused automatically."""
    __tablename__ = "vendor_assignments"

    id = db.Column(db.Integer, primary_key=True)
    vendor_id = db.Column(db.Integer, db.ForeignKey("vendors.id"), nullable=False)
    pm_id = db.Column(db.Integer, db.ForeignKey("staff.id"), nullable=False)
    administrator_id = db.Column(db.Integer, db.ForeignKey("staff.id"), nullable=False)
    contract_number = db.Column(db.String(64))

    pm = db.relationship("Staff", foreign_keys=[pm_id])
    administrator = db.relationship("Staff", foreign_keys=[administrator_id])


class PurchaseOrder(db.Model):
    """Uploaded by a PM or Administrator via the dashboard. Its budget lines
    are remembered and pre-filled onto future invoices for the same PO."""
    __tablename__ = "purchase_orders"

    id = db.Column(db.Integer, primary_key=True)
    po_number = db.Column(db.String(64), nullable=False, index=True)
    vendor_id = db.Column(db.Integer, db.ForeignKey("vendors.id"))
    contract_number = db.Column(db.String(64))
    description = db.Column(db.Text)  # raw text pulled from the PO PDF
    filename = db.Column(db.String(256))
    data = db.Column(db.LargeBinary)
    mimetype = db.Column(db.String(128))
    uploaded_by = db.Column(db.String(128))
    uploaded_at = db.Column(db.DateTime, default=datetime.utcnow)

    # "one_time" POs never need review. "fiscal_year" POs are tied to a
    # specific fiscal year's allocation (City FY runs 7/1-6/30) and should
    # be flagged for review once a new fiscal year starts.
    fiscal_year_scope = db.Column(db.String(16), default="one_time")  # "one_time" | "fiscal_year"
    # e.g. "FY26" for the fiscal year ending 6/30/2026 — set when created,
    # updated when someone reviews/confirms the PO for a new fiscal year.
    fiscal_year_label = db.Column(db.String(16))
    fiscal_year_reviewed_at = db.Column(db.DateTime)

    budget_lines = db.relationship(
        "POBudgetLine", backref="purchase_order", cascade="all, delete-orphan",
        order_by="POBudgetLine.line_number",
    )


class POBudgetLine(db.Model):
    __tablename__ = "po_budget_lines"

    id = db.Column(db.Integer, primary_key=True)
    purchase_order_id = db.Column(
        db.Integer, db.ForeignKey("purchase_orders.id"), nullable=False
    )
    line_number = db.Column(db.Integer)
    account_string = db.Column(db.String(128), nullable=False)
    description = db.Column(db.Text)  # PO line-item text, e.g. "Landscaping - Civic Center Park"
    budgeted_amount = db.Column(db.Numeric(12, 2))


class Invoice(db.Model):
    __tablename__ = "invoices"

    id = db.Column(db.Integer, primary_key=True)

    # Extracted / entered fields
    vendor_id = db.Column(db.Integer, db.ForeignKey("vendors.id"))
    vendor_name_raw = db.Column(db.String(256))  # as it appeared on the PDF/email, before vendor match
    invoice_number = db.Column(db.String(64))
    amount = db.Column(db.Numeric(12, 2))
    po_number = db.Column(db.String(64))
    purchase_order_id = db.Column(db.Integer, db.ForeignKey("purchase_orders.id"))
    due_date = db.Column(db.Date)
    received_at = db.Column(db.DateTime, default=datetime.utcnow)

    # Email provenance
    sender_email = db.Column(db.String(256))
    sender_domain_verified = db.Column(db.Boolean, default=False)  # berkeleyca.gov?
    cc_emails = db.Column(db.Text)
    email_subject = db.Column(db.String(256))
    email_message_id = db.Column(db.String(256))  # for threading the reply

    filename = db.Column(db.String(256))
    data = db.Column(db.LargeBinary, nullable=False)
    mimetype = db.Column(db.String(128))
    extracted_text = db.Column(db.Text)

    # Routing
    pm_id = db.Column(db.Integer, db.ForeignKey("staff.id"))
    administrator_id = db.Column(db.Integer, db.ForeignKey("staff.id"))

    # Status: received -> needs_assignment -> pending_pm_approval ->
    # approved -> entered_in_munis
    status = db.Column(db.String(32), default="received", index=True)

    pm_approved_at = db.Column(db.DateTime)
    pm_approval_note = db.Column(db.Text)  # raw reply text or dashboard note
    munis_entered_at = db.Column(db.DateTime)
    munis_entered_by = db.Column(db.String(128))

    last_reminder_sent_at = db.Column(db.DateTime)

    # Set when a multi-invoice PDF gets split into several Invoice records —
    # points back to the original combined upload for reference.
    split_from_invoice_id = db.Column(db.Integer, db.ForeignKey("invoices.id"))

    pm = db.relationship("Staff", foreign_keys=[pm_id])
    administrator = db.relationship("Staff", foreign_keys=[administrator_id])
    purchase_order = db.relationship("PurchaseOrder")
    coding_lines = db.relationship(
        "InvoiceCodingLine", backref="invoice", cascade="all, delete-orphan",
        order_by="InvoiceCodingLine.line_number",
    )
    events = db.relationship(
        "InvoiceEvent", backref="invoice", cascade="all, delete-orphan",
        order_by="InvoiceEvent.created_at",
    )

    @property
    def status_label(self):
        return {
            "received": "Received",
            "needs_assignment": "Needs PM/Admin Assignment",
            "pending_pm_approval": "Pending PM Approval",
            "approved": "Approved — Ready for Munis",
            "entered_in_munis": "Entered in Munis",
        }.get(self.status, self.status)

    @property
    def urgency(self):
        """"paid" once entered in Munis (no longer worth flagging), else
        based on how the due date compares to today (Pacific): "overdue",
        "due_soon" (within 7 days), "due_later", or "no_date"."""
        if self.status == "entered_in_munis":
            return "paid"
        if not self.due_date:
            return "no_date"
        from datetime import timedelta
        from timezone_utils import today_pacific
        today = today_pacific()
        if self.due_date < today:
            return "overdue"
        if self.due_date <= today + timedelta(days=7):
            return "due_soon"
        return "due_later"

    @property
    def urgency_rank(self):
        return {"overdue": 0, "due_soon": 1, "due_later": 2, "no_date": 3, "paid": 4}[self.urgency]

    @property
    def coding_total(self):
        return sum((line.amount or 0) for line in self.coding_lines)

    @property
    def coding_matches_total(self):
        """None when there's nothing to check against yet (no amount
        detected, or no coding lines entered)."""
        if self.amount is None or not self.coding_lines:
            return None
        return abs(self.coding_total - self.amount) < 0.01


class InvoiceCodingLine(db.Model):
    """The budget-line breakdown for one invoice, pre-filled from the PO's
    budget lines where possible and editable by the PM."""
    __tablename__ = "invoice_coding_lines"

    id = db.Column(db.Integer, primary_key=True)
    invoice_id = db.Column(db.Integer, db.ForeignKey("invoices.id"), nullable=False)
    line_number = db.Column(db.Integer)  # matches the PO's line number when copied from one
    account_string = db.Column(db.String(128), nullable=False)
    description = db.Column(db.Text)  # carried over from the PO budget line
    amount = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    source = db.Column(db.String(16), default="po")  # "po" | "pm_edit"


class InvoiceEvent(db.Model):
    """Audit trail: received, assigned, reminder sent, approved, entered."""
    __tablename__ = "invoice_events"

    id = db.Column(db.Integer, primary_key=True)
    invoice_id = db.Column(db.Integer, db.ForeignKey("invoices.id"), nullable=False)
    event_type = db.Column(db.String(32))
    detail = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class ActiveAccount(db.Model):
    """The City's chart of accounts — every valid 8-segment GL account
    string (Fund-Dept-Division-Subdivision-Program-Future-Activity-Object),
    imported from the COA spreadsheet (see coa_import.py). Powers the
    account-search typeahead on the PO and invoice budget-coding pages so
    staff pick a valid account instead of typing the raw string by hand."""
    __tablename__ = "active_accounts"

    id = db.Column(db.Integer, primary_key=True)
    account = db.Column(db.String(64), unique=True, index=True)
    description = db.Column(db.String(256), index=True)  # object description, e.g. "PROF SVCS - MISCELLANEOUS"
    short_description = db.Column(db.String(64))
    # Every segment's name concatenated (fund, dept, division, sub-division,
    # program, activity, object) so searching "Transportation" or "Parking"
    # or a fund name/number surfaces matches, not just the object type.
    search_text = db.Column(db.Text, index=True)


class SegmentCode(db.Model):
    """Per-segment lookup (fund/dept/division/sub-division/program/activity
    code -> name), imported from the COA spreadsheet's individual sheets.
    Used at import time to build ActiveAccount.search_text."""
    __tablename__ = "segment_codes"

    id = db.Column(db.Integer, primary_key=True)
    segment = db.Column(db.String(16), index=True)  # fund|dept|div|subdiv|program|activity
    code = db.Column(db.String(16), index=True)
    description = db.Column(db.String(128))

    __table_args__ = (
        db.UniqueConstraint("segment", "code", name="uq_segment_code"),
    )


class OutgoingEmailLog(db.Model):
    """In mock mode, sent emails land here instead of hitting the wire, so
    the dashboard/dev can inspect exactly what would have gone out."""
    __tablename__ = "outgoing_email_log"

    id = db.Column(db.Integer, primary_key=True)
    to_email = db.Column(db.String(256))
    subject = db.Column(db.String(256))
    body = db.Column(db.Text)
    invoice_id = db.Column(db.Integer, db.ForeignKey("invoices.id"))
    sent_at = db.Column(db.DateTime, default=datetime.utcnow)

    # Single-attachment support (this app only ever sends one PDF per email).
    attachment_filename = db.Column(db.String(256))
    attachment_data = db.Column(db.LargeBinary)
    attachment_mimetype = db.Column(db.String(128))
