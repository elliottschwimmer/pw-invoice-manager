from datetime import datetime, date
from io import BytesIO

from flask import (
    Flask, render_template, request, redirect, url_for, send_file, flash,
    jsonify, session,
)

from config import Config
from models import (
    db, Invoice, Vendor, Staff, VendorAssignment, PurchaseOrder, POBudgetLine,
    OutgoingEmailLog, ActiveAccount,
)
from intake import (
    ingest_new_invoices, create_invoice_from_upload, assign_invoice, approve_invoice,
    mark_entered_in_munis, send_pm_reminder, correct_vendor, link_purchase_order,
    update_coding_lines as _apply_coding_lines,
)
from pdf_export import generate_final_pdf, generate_stamped_pdf
from timezone_utils import format_pacific
from coding_suggest import suggest_coding_lines
from fiscal_year_utils import current_fiscal_year_label, po_needs_fiscal_year_review
from po_import import import_munis_po


def _money(value):
    if value is None:
        return "—"
    return "${:,.2f}".format(value)


def create_app():
    app = Flask(__name__)
    app.config.from_object(Config)
    app.jinja_env.filters["money"] = _money
    app.jinja_env.filters["pacific"] = format_pacific
    app.jinja_env.globals["po_needs_fy_review"] = po_needs_fiscal_year_review
    app.jinja_env.globals["current_fiscal_year_label"] = current_fiscal_year_label
    db.init_app(app)

    with app.app_context():
        db.create_all()

    register_auth(app)
    register_routes(app)
    return app


def register_auth(app):
    """Gates the whole app behind a single shared password when
    APP_PASSWORD is set (i.e. once deployed) — locally it's unset, so
    every request just passes through untouched."""

    @app.before_request
    def require_login():
        password = app.config.get("APP_PASSWORD")
        if not password:
            return None
        if request.endpoint in ("login", "static"):
            return None
        if session.get("authed"):
            return None
        return redirect(url_for("login", next=request.path))

    @app.route("/login", methods=["GET", "POST"])
    def login():
        error = None
        if request.method == "POST":
            if request.form.get("password") == app.config.get("APP_PASSWORD"):
                session["authed"] = True
                return redirect(request.form.get("next") or url_for("dashboard"))
            error = "Incorrect password"
        return render_template("login.html", error=error, next=request.args.get("next", ""))

    @app.route("/logout", methods=["POST"])
    def logout():
        session.clear()
        return redirect(url_for("login"))


def register_routes(app):
    @app.route("/")
    def dashboard():
        status_filter = request.args.get("status")
        urgency_filter = request.args.get("urgency")

        query = Invoice.query
        if status_filter:
            query = query.filter_by(status=status_filter)
        invoices = query.all()

        # Urgency counts reflect the current status filter (if any) so the
        # chips stay meaningful together, but are computed before the
        # urgency filter itself is applied.
        urgency_counts = {"overdue": 0, "due_soon": 0, "due_later": 0, "no_date": 0, "paid": 0}
        for inv in invoices:
            urgency_counts[inv.urgency] += 1

        if urgency_filter:
            invoices = [inv for inv in invoices if inv.urgency == urgency_filter]

        # Default sort: most urgent first (overdue, then due this week, then
        # later, then no date, then already paid), soonest due date within
        # each group. Falls back to received date when there's no due date.
        invoices.sort(key=lambda inv: (
            inv.urgency_rank,
            inv.due_date or date.max,
            -(inv.received_at.timestamp() if inv.received_at else 0),
        ))

        counts = {
            "all": Invoice.query.count(),
            "needs_assignment": Invoice.query.filter_by(status="needs_assignment").count(),
            "pending_pm_approval": Invoice.query.filter_by(status="pending_pm_approval").count(),
            "approved": Invoice.query.filter_by(status="approved").count(),
            "entered_in_munis": Invoice.query.filter_by(status="entered_in_munis").count(),
        }

        pos_needing_fy_review = [
            po for po in PurchaseOrder.query.filter_by(fiscal_year_scope="fiscal_year").all()
            if po_needs_fiscal_year_review(po)
        ]

        return render_template(
            "dashboard.html", invoices=invoices, counts=counts, active_status=status_filter,
            urgency_counts=urgency_counts, active_urgency=urgency_filter,
            pos_needing_fy_review=pos_needing_fy_review,
        )

    @app.route("/invoices/<int:invoice_id>")
    def invoice_detail(invoice_id):
        invoice = Invoice.query.get_or_404(invoice_id)
        staff = Staff.query.order_by(Staff.name).all()
        vendors = Vendor.query.order_by(Vendor.name).all()
        purchase_orders_list = PurchaseOrder.query.order_by(PurchaseOrder.po_number).all()
        return render_template(
            "invoice_detail.html", invoice=invoice, staff=staff, vendors=vendors,
            purchase_orders_list=purchase_orders_list, today=date.today(),
        )

    @app.route("/invoices/<int:invoice_id>/link-po", methods=["POST"])
    def link_po(invoice_id):
        invoice = Invoice.query.get_or_404(invoice_id)
        po_id = request.form.get("purchase_order_id")
        if po_id:
            link_purchase_order(invoice, int(po_id))
            flash("Invoice linked to PO — budget lines copied in")
        return redirect(url_for("invoice_detail", invoice_id=invoice_id))

    @app.route("/invoices/<int:invoice_id>/vendor", methods=["POST"])
    def update_vendor(invoice_id):
        invoice = Invoice.query.get_or_404(invoice_id)
        correct_vendor(invoice, request.form.get("vendor_name", ""))
        flash("Vendor updated")
        return redirect(url_for("invoice_detail", invoice_id=invoice_id))

    @app.route("/invoices/<int:invoice_id>/pdf")
    def invoice_pdf(invoice_id):
        invoice = Invoice.query.get_or_404(invoice_id)
        return send_file(
            BytesIO(invoice.data), mimetype="application/pdf",
            as_attachment=False, download_name=invoice.filename or "invoice.pdf",
        )

    @app.route("/invoices/<int:invoice_id>/final-pdf")
    def invoice_final_pdf(invoice_id):
        invoice = Invoice.query.get_or_404(invoice_id)
        pdf_bytes = generate_final_pdf(invoice)
        vendor_name = (invoice.vendor.name if invoice.vendor else invoice.vendor_name_raw or "invoice").replace(" ", "_")
        download_name = f"{vendor_name}_{invoice.invoice_number or invoice.id}_approved.pdf"
        return send_file(
            BytesIO(pdf_bytes), mimetype="application/pdf",
            as_attachment=False, download_name=download_name,
        )

    @app.route("/invoices/<int:invoice_id>/stamped-pdf")
    def invoice_stamped_pdf(invoice_id):
        invoice = Invoice.query.get_or_404(invoice_id)
        pdf_bytes = generate_stamped_pdf(invoice)
        vendor_name = (invoice.vendor.name if invoice.vendor else invoice.vendor_name_raw or "invoice").replace(" ", "_")
        download_name = f"{vendor_name}_{invoice.invoice_number or invoice.id}_stamped.pdf"
        return send_file(
            BytesIO(pdf_bytes), mimetype="application/pdf",
            as_attachment=False, download_name=download_name,
        )

    @app.route("/invoices/<int:invoice_id>/assign", methods=["POST"])
    def assign(invoice_id):
        invoice = Invoice.query.get_or_404(invoice_id)
        pm = Staff.query.get(request.form["pm_id"])
        administrator = Staff.query.get(request.form["administrator_id"])
        remember = bool(request.form.get("remember"))
        assign_invoice(invoice, pm, administrator, remember=remember)
        flash(f"Assigned to {pm.name} (PM) / {administrator.name} (Administrator)")
        return redirect(url_for("invoice_detail", invoice_id=invoice_id))

    @app.route("/invoices/<int:invoice_id>/remind", methods=["POST"])
    def remind(invoice_id):
        invoice = Invoice.query.get_or_404(invoice_id)
        send_pm_reminder(invoice)
        flash(f"Reminder sent to {invoice.pm.name if invoice.pm else 'PM'}")
        return redirect(url_for("invoice_detail", invoice_id=invoice_id))

    @app.route("/invoices/<int:invoice_id>/approve", methods=["POST"])
    def approve(invoice_id):
        invoice = Invoice.query.get_or_404(invoice_id)
        note = request.form.get("note", "")
        mismatch = invoice.coding_matches_total is False
        approve_invoice(invoice, note=note)
        if mismatch:
            flash(
                f"Invoice approved, but budget lines total {_money(invoice.coding_total)} while "
                f"the invoice amount is {_money(invoice.amount)} — please double check the coding."
            )
        else:
            flash("Invoice approved and Administrator notified")
        return redirect(url_for("invoice_detail", invoice_id=invoice_id))

    @app.route("/invoices/<int:invoice_id>/mark-entered", methods=["POST"])
    def mark_entered(invoice_id):
        invoice = Invoice.query.get_or_404(invoice_id)
        entered_by = request.form.get("entered_by", "Administrator")
        mark_entered_in_munis(invoice, entered_by)
        flash("Marked entered in Munis")
        return redirect(url_for("invoice_detail", invoice_id=invoice_id))

    @app.route("/invoices/<int:invoice_id>/due-date", methods=["POST"])
    def set_due_date(invoice_id):
        invoice = Invoice.query.get_or_404(invoice_id)
        due_date_str = request.form.get("due_date")
        if due_date_str:
            invoice.due_date = datetime.strptime(due_date_str, "%Y-%m-%d").date()
            db.session.commit()
        return redirect(url_for("invoice_detail", invoice_id=invoice_id))

    @app.route("/invoices/<int:invoice_id>/suggest-coding", methods=["POST"])
    def suggest_coding(invoice_id):
        invoice = Invoice.query.get_or_404(invoice_id)
        suggestions = suggest_coding_lines(invoice)
        if not suggestions:
            flash("No confident PO-line matches found — code this one manually.")
            return redirect(url_for("invoice_detail", invoice_id=invoice_id))
        _apply_coding_lines(invoice, suggestions)
        flash(f"Suggested {len(suggestions)} budget line(s) from the PO — review before approving.")
        return redirect(url_for("invoice_detail", invoice_id=invoice_id))

    @app.route("/invoices/<int:invoice_id>/coding-lines", methods=["POST"])
    def update_coding_lines(invoice_id):
        invoice = Invoice.query.get_or_404(invoice_id)
        lines = []
        i = 0
        while f"account_{i}" in request.form:
            account = request.form[f"account_{i}"].strip()
            description = request.form.get(f"description_{i}", "").strip()
            amount_raw = request.form.get(f"amount_{i}", "").strip()
            line_no_raw = request.form.get(f"line_number_{i}", "").strip()
            if account or description or amount_raw or line_no_raw:
                line_no = int(line_no_raw) if line_no_raw else (len(lines) + 1)
                lines.append(
                    {
                        "line_number": line_no,
                        "account_string": account or None,
                        "description": description,
                        "amount": amount_raw or 0,
                    }
                )
            i += 1
        _apply_coding_lines(invoice, lines)

        invoice = Invoice.query.get(invoice_id)
        if invoice.coding_matches_total is False:
            flash(
                f"Budget coding updated — WARNING: lines total {_money(invoice.coding_total)} "
                f"but the invoice amount is {_money(invoice.amount)}. Please correct before approving."
            )
        else:
            flash("Budget coding updated")
        return redirect(url_for("invoice_detail", invoice_id=invoice_id))

    @app.route("/check-mail", methods=["POST"])
    def check_mail():
        created = ingest_new_invoices()
        flash(f"Ingested {len(created)} new invoice(s)")
        return redirect(url_for("dashboard"))

    @app.route("/invoices/upload", methods=["GET", "POST"])
    def upload_invoice():
        if request.method == "GET":
            return render_template("upload_invoice.html")

        file = request.files.get("invoice_file")
        if not file or not file.filename:
            flash("Choose a PDF to upload")
            return redirect(url_for("upload_invoice"))

        uploaded_by = request.form.get("uploaded_by", "")
        created = create_invoice_from_upload(file.read(), file.filename, uploaded_by)

        if len(created) == 1:
            flash("Invoice uploaded")
            return redirect(url_for("invoice_detail", invoice_id=created[0].id))
        flash(f"Uploaded — {len(created)} separate invoices detected in this PDF")
        return redirect(url_for("dashboard"))

    # --- Chart of accounts search (used by the budget-coding typeahead) ---

    @app.route("/api/accounts/search")
    def search_accounts():
        q = (request.args.get("q") or "").strip()
        if len(q) < 2:
            return jsonify([])
        like = f"%{q}%"
        matches = (
            ActiveAccount.query
            .filter(db.or_(
                ActiveAccount.account.ilike(like),
                ActiveAccount.search_text.ilike(like),
            ))
            .order_by(ActiveAccount.account)
            .limit(25)
            .all()
        )
        return jsonify([
            {
                "account": a.account,
                "description": a.description,
                # Full fund/dept/division/.../object trail, e.g.
                # "GF - DISCRETIONARY | PUBLIC WORKS | ENGINEERING | ... | PROF SVCS - MISCELLANEOUS"
                "context": a.search_text,
            }
            for a in matches
        ])

    # --- Purchase orders ---------------------------------------------------

    @app.route("/purchase-orders")
    def purchase_orders():
        pos = PurchaseOrder.query.order_by(PurchaseOrder.uploaded_at.desc()).all()
        return render_template("purchase_orders.html", pos=pos)

    @app.route("/purchase-orders/create", methods=["POST"])
    def create_po():
        po_number = request.form["po_number"]
        vendor_name = request.form.get("vendor_name", "").strip()
        contract_number = request.form.get("contract_number", "")
        uploaded_by = request.form.get("uploaded_by", "")
        fiscal_year_scope = request.form.get("fiscal_year_scope", "one_time")

        po = PurchaseOrder(
            po_number=po_number,
            vendor_name=vendor_name or None,
            contract_number=contract_number,
            uploaded_by=uploaded_by,
            fiscal_year_scope=fiscal_year_scope,
        )
        if fiscal_year_scope == "fiscal_year":
            po.fiscal_year_label = current_fiscal_year_label()
            po.fiscal_year_reviewed_at = datetime.utcnow()
        db.session.add(po)
        db.session.commit()
        flash(f"PO {po_number} created — enter its budget lines below.")
        return redirect(url_for("edit_po", po_id=po.id))

    @app.route("/purchase-orders/import-munis", methods=["POST"])
    def import_munis():
        file = request.files.get("munis_file")
        if not file or not file.filename:
            flash("Choose a Munis PO export (.xlsx) to import")
            return redirect(url_for("purchase_orders"))
        uploaded_by = request.form.get("uploaded_by", "")
        try:
            po = import_munis_po(file.read(), uploaded_by)
        except Exception as e:
            flash(f"Couldn't import that file: {e}")
            return redirect(url_for("purchase_orders"))
        flash(f"Imported PO {po.po_number} from Munis — {len(po.budget_lines)} line(s). Account strings still need to be coded.")
        return redirect(url_for("edit_po", po_id=po.id))

    @app.route("/purchase-orders/<int:po_id>")
    def edit_po(po_id):
        po = PurchaseOrder.query.get_or_404(po_id)
        return render_template("po_detail.html", po=po)

    @app.route("/purchase-orders/<int:po_id>/vendor", methods=["POST"])
    def update_po_vendor(po_id):
        po = PurchaseOrder.query.get_or_404(po_id)
        po.vendor_name = request.form.get("vendor_name", "").strip() or None
        db.session.commit()
        flash("Vendor updated")
        return redirect(url_for("edit_po", po_id=po_id))

    @app.route("/purchase-orders/<int:po_id>/lines", methods=["POST"])
    def update_po_lines(po_id):
        po = PurchaseOrder.query.get_or_404(po_id)
        existing_by_number = {bl.line_number: bl for bl in po.budget_lines}
        submitted_numbers = set()

        i = 0
        while f"account_{i}" in request.form:
            account = request.form[f"account_{i}"].strip()
            description = request.form.get(f"description_{i}", "").strip()
            amount_raw = request.form.get(f"amount_{i}", "").strip()
            line_no_raw = request.form.get(f"line_number_{i}", "").strip()

            # A row counts as "used" if it has a line number, an account
            # string, a description, or an amount — the account string
            # alone is never required (Munis-imported lines don't have
            # one until a PM codes them, and that shouldn't erase the row).
            if account or description or amount_raw or line_no_raw:
                line_no = int(line_no_raw) if line_no_raw else (i + 1)
                submitted_numbers.add(line_no)
                existing = existing_by_number.get(line_no)
                if existing:
                    existing.account_string = account or None
                    existing.description = description
                    existing.budgeted_amount = amount_raw or 0
                else:
                    db.session.add(
                        POBudgetLine(
                            purchase_order_id=po.id,
                            line_number=line_no,
                            account_string=account or None,
                            description=description,
                            budgeted_amount=amount_raw or 0,
                        )
                    )
            i += 1

        # Remove lines that were deleted from the form (but weren't part of
        # a Munis import — those should be re-imported, not hand-deleted).
        for line_no, existing in existing_by_number.items():
            if line_no not in submitted_numbers:
                db.session.delete(existing)

        db.session.commit()
        flash("Budget lines updated")
        return redirect(url_for("edit_po", po_id=po.id))

    @app.route("/purchase-orders/<int:po_id>/review-fy", methods=["POST"])
    def review_po_fy(po_id):
        po = PurchaseOrder.query.get_or_404(po_id)
        po.fiscal_year_label = current_fiscal_year_label()
        po.fiscal_year_reviewed_at = datetime.utcnow()
        db.session.commit()
        flash(f"PO {po.po_number} marked reviewed for {current_fiscal_year_label()}")
        return redirect(request.referrer or url_for("purchase_orders"))

    # --- Staff (PMs / Administrators) --------------------------------------

    @app.route("/staff")
    def staff_list():
        staff = Staff.query.order_by(Staff.role, Staff.name).all()
        return render_template("staff.html", staff=staff)

    @app.route("/staff/add", methods=["POST"])
    def add_staff():
        db.session.add(
            Staff(
                name=request.form["name"],
                email=request.form["email"],
                role=request.form["role"],
                division=request.form.get("division", ""),
                sub_division=request.form.get("sub_division", ""),
            )
        )
        db.session.commit()
        flash("Staff member added")
        return redirect(url_for("staff_list"))

    # --- Outbox --------------------------------------------------------

    @app.route("/outbox")
    def outbox():
        emails = OutgoingEmailLog.query.order_by(OutgoingEmailLog.sent_at.desc()).all()
        return render_template("outbox.html", emails=emails)

    @app.route("/outbox/<int:email_id>/attachment")
    def outbox_attachment(email_id):
        email = OutgoingEmailLog.query.get_or_404(email_id)
        if not email.attachment_data:
            return redirect(url_for("outbox"))
        return send_file(
            BytesIO(email.attachment_data), mimetype=email.attachment_mimetype or "application/pdf",
            as_attachment=False, download_name=email.attachment_filename or "attachment.pdf",
        )

    # --- Vendors -------------------------------------------------------

    @app.route("/vendors")
    def vendors_list():
        vendors = Vendor.query.order_by(Vendor.name).all()
        rows = []
        for v in vendors:
            invoices = v.invoices
            rows.append({
                "vendor": v,
                "invoice_count": len(invoices),
                "total_amount": sum((inv.amount or 0) for inv in invoices),
                "open_count": sum(1 for inv in invoices if inv.status != "entered_in_munis"),
            })
        rows.sort(key=lambda r: r["vendor"].name.lower())
        return render_template("vendors.html", rows=rows)

    @app.route("/vendors/<int:vendor_id>")
    def vendor_detail(vendor_id):
        vendor = Vendor.query.get_or_404(vendor_id)
        invoices = sorted(vendor.invoices, key=lambda inv: inv.received_at or datetime.min, reverse=True)
        return render_template("vendor_detail.html", vendor=vendor, invoices=invoices)


app = create_app()

if __name__ == "__main__":
    app.run(debug=True, port=5051)
