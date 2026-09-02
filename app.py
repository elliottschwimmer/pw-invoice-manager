import re
from datetime import datetime, date
from decimal import Decimal
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
    unapprove_invoice, mark_entered_in_munis, send_pm_reminder, correct_vendor, link_purchase_order,
    update_coding_lines as _apply_coding_lines,
)
from pdf_export import generate_final_pdf, generate_stamped_pdf
from timezone_utils import format_pacific
from coding_suggest import suggest_coding_lines, remaining_budget
from fiscal_year_utils import (
    current_fiscal_year_label, po_needs_fiscal_year_review, current_fiscal_year_end_date,
)
from po_import import import_munis_po


def _money(value):
    if value is None:
        return "—"
    return "${:,.2f}".format(value)


def _clean_amount(raw: str) -> str:
    """Amount fields are comma-formatted text inputs (for display), so
    strip $ and , before this ever reaches a Numeric column."""
    return raw.replace("$", "").replace(",", "").strip()


def _normalize_vendor_name(name: str) -> str:
    """Lowercase, punctuation-insensitive, whitespace-collapsed form of a
    vendor name — used only to spot likely duplicates ("Turnstone Data
    Inc." vs "Turnstone Data, Inc" vs "turnstone data inc"), never stored."""
    stripped = re.sub(r"[^\w\s]", "", (name or "").lower())
    return re.sub(r"\s+", " ", stripped).strip()


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
        pm_filter = request.args.get("pm_id", type=int)
        vendor_filter = request.args.get("vendor_id", type=int)

        query = Invoice.query
        if status_filter:
            query = query.filter_by(status=status_filter)
        if pm_filter:
            query = query.filter_by(pm_id=pm_filter)
        if vendor_filter:
            query = query.filter_by(vendor_id=vendor_filter)
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

        # Scoped to the selected PM/vendor (if any) so the status chips
        # reflect just that filtered set, not the department-wide totals.
        counts_query = Invoice.query
        if pm_filter:
            counts_query = counts_query.filter_by(pm_id=pm_filter)
        if vendor_filter:
            counts_query = counts_query.filter_by(vendor_id=vendor_filter)
        counts = {
            "all": counts_query.count(),
            "needs_assignment": counts_query.filter_by(status="needs_assignment").count(),
            "pending_pm_approval": counts_query.filter_by(status="pending_pm_approval").count(),
            "approved": counts_query.filter_by(status="approved").count(),
            "entered_in_munis": counts_query.filter_by(status="entered_in_munis").count(),
        }

        pos_needing_fy_review = [
            po for po in PurchaseOrder.query.filter_by(fiscal_year_scope="fiscal_year").all()
            if po_needs_fiscal_year_review(po)
        ]

        pms = Staff.query.filter_by(role="pm").order_by(Staff.name).all()
        vendors_for_filter = Vendor.query.order_by(Vendor.name).all()

        return render_template(
            "dashboard.html", invoices=invoices, counts=counts, active_status=status_filter,
            urgency_counts=urgency_counts, active_urgency=urgency_filter,
            pos_needing_fy_review=pos_needing_fy_review,
            pms=pms, active_pm=pm_filter,
            vendors_for_filter=vendors_for_filter, active_vendor=vendor_filter,
        )

    @app.route("/invoices/<int:invoice_id>")
    def invoice_detail(invoice_id):
        invoice = Invoice.query.get_or_404(invoice_id)
        staff = Staff.query.order_by(Staff.name).all()
        vendors = Vendor.query.order_by(Vendor.name).all()
        purchase_orders_list = PurchaseOrder.query.order_by(PurchaseOrder.po_number).all()

        # Per-PO-line remaining budget, excluding whatever this invoice has
        # already coded to it — used to show a before/after progress bar
        # next to each budget-coding row as the PM enters an amount.
        budget_progress = {}
        if invoice.purchase_order:
            for pl in invoice.purchase_order.budget_lines:
                budgeted = float(pl.budgeted_amount or 0)
                remaining_before = float(remaining_budget(pl, exclude_invoice_id=invoice.id))
                budget_progress[pl.line_number] = {
                    "budgeted": budgeted,
                    "remaining_before": remaining_before,
                }

        return render_template(
            "invoice_detail.html", invoice=invoice, staff=staff, vendors=vendors,
            purchase_orders_list=purchase_orders_list, today=date.today(),
            budget_progress=budget_progress,
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

    @app.route("/invoices/<int:invoice_id>/unapprove", methods=["POST"])
    def unapprove(invoice_id):
        invoice = Invoice.query.get_or_404(invoice_id)
        note = request.form.get("note", "")
        unapprove_invoice(invoice, note=note)
        flash("Invoice pulled back to Pending PM Approval — budget coding is open for corrections again")
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

    @app.route("/invoices/<int:invoice_id>/amount", methods=["POST"])
    def set_amount(invoice_id):
        invoice = Invoice.query.get_or_404(invoice_id)
        amount_raw = _clean_amount(request.form.get("amount", ""))
        invoice.amount = amount_raw or None
        db.session.commit()
        flash("Invoice amount updated")
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
        # Account string is no longer edited on this page — preserve whatever
        # was already stored per line_number (inherited from the linked PO),
        # falling back to the PO's own budget line for one newly added via
        # the "add a PO line" picker that hasn't been coded on this invoice
        # before — rather than wiping it, since the form no longer submits
        # an account string field at all.
        existing_accounts_by_line = {
            cl.line_number: cl.account_string for cl in invoice.coding_lines
        }
        po_accounts_by_line = {}
        if invoice.purchase_order:
            po_accounts_by_line = {
                pl.line_number: pl.account_string for pl in invoice.purchase_order.budget_lines
            }
        lines = []
        # A fixed range rather than "stop at the first missing index" — the
        # PM can delete a row from the middle of the table (via the row's
        # remove button), which leaves a gap in the submitted field
        # indices; stopping at that gap would silently drop every row after
        # it instead of just the deleted one.
        for i in range(50):
            if (
                f"line_number_{i}" not in request.form
                and f"description_{i}" not in request.form
                and f"amount_{i}" not in request.form
            ):
                continue
            description = request.form.get(f"description_{i}", "").strip()
            amount_raw = _clean_amount(request.form.get(f"amount_{i}", ""))
            line_no_raw = request.form.get(f"line_number_{i}", "").strip()
            if description or amount_raw or line_no_raw:
                line_no = int(line_no_raw) if line_no_raw else (len(lines) + 1)
                lines.append(
                    {
                        "line_number": line_no,
                        "account_string": existing_accounts_by_line.get(line_no) or po_accounts_by_line.get(line_no),
                        "description": description,
                        "amount": amount_raw or 0,
                    }
                )
        _apply_coding_lines(invoice, lines)

        invoice = Invoice.query.get(invoice_id)
        if invoice.coding_matches_total is False:
            message = (
                f"Budget coding updated — WARNING: lines total {_money(invoice.coding_total)} "
                f"but the invoice amount is {_money(invoice.amount)}. Please correct before approving."
            )
        else:
            message = "Budget coding updated"

        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return {
                "ok": True,
                "message": message,
                "matches_total": invoice.coding_matches_total,
            }

        flash(message)
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

        # Fiscal-year POs still carrying unspent budget as the fiscal year
        # winds down — a heads-up to spend it down or carry it forward
        # deliberately, not just let it lapse unnoticed.
        fy_end = current_fiscal_year_end_date()
        days_left = (fy_end - date.today()).days
        expiring_pos = []
        if 0 <= days_left <= 60:
            for po in pos:
                if po.fiscal_year_scope != "fiscal_year":
                    continue
                if po.fiscal_year_label != current_fiscal_year_label():
                    continue  # already surfaced by the FY-review flag instead
                po_remaining = sum(
                    (remaining_budget(pl) for pl in po.budget_lines), Decimal("0")
                )
                if po_remaining > 0:
                    expiring_pos.append({"po": po, "remaining": po_remaining})

        return render_template(
            "purchase_orders.html", pos=pos, expiring_pos=expiring_pos,
            fy_end=fy_end, days_left=days_left,
        )

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

        # Budget burn-down: how much of each line (and the PO overall) has
        # actually been spent via coded invoices, vs. what's left.
        budget_lines_progress = []
        total_budgeted = Decimal("0")
        total_spent = Decimal("0")
        for pl in po.budget_lines:
            budgeted = pl.budgeted_amount or Decimal("0")
            remaining = remaining_budget(pl)
            spent = budgeted - remaining
            pct = float(spent / budgeted * 100) if budgeted else 0.0
            pct = max(0.0, min(100.0, pct))
            budget_lines_progress.append({
                "line_number": pl.line_number, "budgeted": budgeted,
                "spent": spent, "remaining": remaining, "pct": pct,
            })
            total_budgeted += budgeted
            total_spent += spent
        total_remaining = total_budgeted - total_spent
        total_pct = float(total_spent / total_budgeted * 100) if total_budgeted else 0.0
        total_pct = max(0.0, min(100.0, total_pct))

        return render_template(
            "po_detail.html", po=po, budget_lines_progress=budget_lines_progress,
            total_budgeted=total_budgeted, total_spent=total_spent,
            total_remaining=total_remaining, total_pct=total_pct,
        )

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
            amount_raw = _clean_amount(request.form.get(f"amount_{i}", ""))
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
                # One insert/update at a time — avoids a SQLAlchemy/psycopg2
                # batched-insert mismatch seen with multiple new rows at once.
                db.session.flush()
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
                "po_count": len(v.purchase_orders),
            })
        rows.sort(key=lambda r: r["vendor"].name.lower())

        # Group vendors whose names normalize to the same thing (case,
        # punctuation, and whitespace differences only) — the common shape
        # of a duplicate vendor entry — so staff can merge them with one
        # click instead of hunting through the full list by eye.
        groups = {}
        for r in rows:
            groups.setdefault(_normalize_vendor_name(r["vendor"].name), []).append(r)
        duplicate_groups = [g for g in groups.values() if len(g) > 1]
        duplicate_groups.sort(key=lambda g: g[0]["vendor"].name.lower())

        return render_template("vendors.html", rows=rows, duplicate_groups=duplicate_groups)

    @app.route("/vendors/merge", methods=["POST"])
    def merge_vendors():
        keep_id = request.form.get("keep_vendor_id", type=int)
        merge_ids = [int(v) for v in request.form.getlist("merge_vendor_ids") if v]
        merge_ids = [v for v in merge_ids if v != keep_id]
        if not keep_id or not merge_ids:
            flash("Choose which vendor to keep and at least one duplicate to merge into it.")
            return redirect(url_for("vendors_list"))

        keep = Vendor.query.get_or_404(keep_id)
        merged_names = []
        for dup_id in merge_ids:
            dup = Vendor.query.get(dup_id)
            if not dup:
                continue
            merged_names.append(dup.name)
            Invoice.query.filter_by(vendor_id=dup.id).update({"vendor_id": keep.id})
            PurchaseOrder.query.filter_by(vendor_id=dup.id).update({"vendor_id": keep.id})
            VendorAssignment.query.filter_by(vendor_id=dup.id).update({"vendor_id": keep.id})
            db.session.flush()
            db.session.delete(dup)
        db.session.commit()
        flash(f"Merged {', '.join(merged_names)} into {keep.name}")
        return redirect(url_for("vendors_list"))

    @app.route("/vendors/<int:vendor_id>/rename", methods=["POST"])
    def rename_vendor(vendor_id):
        vendor = Vendor.query.get_or_404(vendor_id)
        new_name = request.form.get("name", "").strip()
        if new_name:
            vendor.name = new_name
            db.session.commit()
            message = "Vendor renamed"
            ok = True
        else:
            message = "Vendor name can't be blank"
            ok = False

        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return {"ok": ok, "message": message, "name": vendor.name}
        flash(message)
        return redirect(url_for("vendors_list"))

    @app.route("/vendors/<int:vendor_id>/delete", methods=["POST"])
    def delete_vendor(vendor_id):
        vendor = Vendor.query.get_or_404(vendor_id)
        if vendor.invoices or vendor.purchase_orders:
            flash(f"Can't delete {vendor.name} — it still has invoices or POs. Merge it into another vendor instead.")
            return redirect(url_for("vendors_list"))
        VendorAssignment.query.filter_by(vendor_id=vendor.id).delete()
        name = vendor.name
        db.session.delete(vendor)
        db.session.commit()
        flash(f"Deleted {name}")
        return redirect(url_for("vendors_list"))

    @app.route("/vendors/<int:vendor_id>")
    def vendor_detail(vendor_id):
        vendor = Vendor.query.get_or_404(vendor_id)
        invoices = sorted(vendor.invoices, key=lambda inv: inv.received_at or datetime.min, reverse=True)
        return render_template("vendor_detail.html", vendor=vendor, invoices=invoices)


app = create_app()

if __name__ == "__main__":
    app.run(debug=True, port=5051)
