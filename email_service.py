"""Send/receive email through a swappable backend.

EMAIL_MODE=mock  -> reads .pdf files dropped in EMAIL_DROP_DIR as if they
                    were new invoice emails, and logs outgoing mail to the
                    OutgoingEmailLog table instead of sending it. Use this
                    for local development and demoing to your Fiscal
                    Manager before IT sets up the mailbox/app registration.
EMAIL_MODE=graph -> real Microsoft Graph API against the pw-invoices@
                    mailbox. Requires GRAPH_TENANT_ID / GRAPH_CLIENT_ID /
                    GRAPH_CLIENT_SECRET from an Azure App Registration IT
                    creates for this pilot (Mail.Read, Mail.Send scopes on
                    the shared mailbox).
"""
import os
from datetime import datetime

from flask import current_app

from models import db, OutgoingEmailLog


class MockEmailBackend:
    def fetch_new_messages(self):
        """Return a list of dicts: {filename, data, sender_email, subject,
        cc_emails, message_id} for each PDF sitting in EMAIL_DROP_DIR. Moves
        each file to a 'processed' subfolder after reading so it isn't
        re-ingested."""
        drop_dir = current_app.config["EMAIL_DROP_DIR"]
        os.makedirs(drop_dir, exist_ok=True)
        processed_dir = os.path.join(drop_dir, "processed")
        os.makedirs(processed_dir, exist_ok=True)

        messages = []
        for fname in sorted(os.listdir(drop_dir)):
            if not fname.lower().endswith(".pdf"):
                continue
            path = os.path.join(drop_dir, fname)
            with open(path, "rb") as f:
                data = f.read()
            messages.append(
                {
                    "filename": fname,
                    "data": data,
                    # Convention for local testing: name a file like
                    # "acmecorp__billing@acmecorp.com.pdf" to simulate the
                    # sender address; otherwise a placeholder is used.
                    "sender_email": _guess_sender(fname),
                    "subject": f"Invoice — {fname}",
                    "cc_emails": "",
                    "message_id": f"mock-{fname}",
                }
            )
            os.rename(path, os.path.join(processed_dir, fname))
        return messages

    def send_email(self, to_email, subject, body, invoice_id=None, in_reply_to=None, attachment=None):
        log = OutgoingEmailLog(
            to_email=to_email, subject=subject, body=body, invoice_id=invoice_id
        )
        if attachment:
            log.attachment_filename = attachment["filename"]
            log.attachment_data = attachment["data"]
            log.attachment_mimetype = attachment.get("mimetype", "application/pdf")
        db.session.add(log)
        db.session.commit()
        current_app.logger.info(
            "[MOCK EMAIL] to=%s subject=%s attachment=%s",
            to_email, subject, attachment["filename"] if attachment else None,
        )


class GraphEmailBackend:
    """Stub — wire this up once IT has issued Graph API credentials and the
    shared mailbox exists. Needs: msal (auth) + requests (Graph calls)."""

    def __init__(self, tenant_id, client_id, client_secret, mailbox):
        self.tenant_id = tenant_id
        self.client_id = client_id
        self.client_secret = client_secret
        self.mailbox = mailbox

    def fetch_new_messages(self):
        raise NotImplementedError(
            "Graph API mode not yet wired up — set EMAIL_MODE=mock for now, "
            "or implement this once GRAPH_* env vars are provided by IT."
        )

    def send_email(self, to_email, subject, body, invoice_id=None, in_reply_to=None, attachment=None):
        raise NotImplementedError(
            "Graph API mode not yet wired up — set EMAIL_MODE=mock for now. "
            "Real implementation should base64-encode `attachment['data']` "
            "into the Graph sendMail payload's fileAttachments array."
        )


def _guess_sender(filename: str) -> str:
    if "__" in filename:
        return filename.split("__", 1)[1].rsplit(".", 1)[0]
    return "unknown-vendor@example.com"


def get_email_backend():
    mode = current_app.config.get("EMAIL_MODE", "mock")
    if mode == "graph":
        return GraphEmailBackend(
            current_app.config["GRAPH_TENANT_ID"],
            current_app.config["GRAPH_CLIENT_ID"],
            current_app.config["GRAPH_CLIENT_SECRET"],
            current_app.config["INTAKE_MAILBOX"],
        )
    return MockEmailBackend()
