"""Send/receive email through a swappable backend.

EMAIL_MODE=mock  -> reads .pdf files dropped in EMAIL_DROP_DIR as if they
                    were new invoice emails, and logs outgoing mail to the
                    OutgoingEmailLog table instead of sending it. Use this
                    for local development and demoing to your Fiscal
                    Manager before IT sets up the mailbox/app registration.
EMAIL_MODE=graph -> real Microsoft Graph API against the pw-invoices@
                    mailbox. Requires GRAPH_TENANT_ID / GRAPH_CLIENT_ID /
                    GRAPH_CLIENT_SECRET from an Azure App Registration IT
                    creates for this pilot (Mail.Read, Mail.Send *application*
                    permissions, scoped to just this mailbox via an Exchange
                    application access policy). Once set, app.py's mail
                    poller checks the mailbox automatically on a timer
                    (MAIL_POLL_INTERVAL_MINUTES) — no need to click "Check
                    for new invoices" by hand anymore.
"""
import base64
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


_GRAPH_BASE = "https://graph.microsoft.com/v1.0"


class GraphEmailBackend:
    """Real Microsoft Graph API against the shared pw-invoices@ mailbox.
    Requires an Azure App Registration with Mail.Read + Mail.Send
    *application* permissions, scoped to just this mailbox via an
    Exchange application access policy (see the IT request doc)."""

    def __init__(self, tenant_id, client_id, client_secret, mailbox):
        self.tenant_id = tenant_id
        self.client_id = client_id
        self.client_secret = client_secret
        self.mailbox = mailbox
        self._token = None

    def _access_token(self) -> str:
        # Cached per-backend-instance (one per request/poll tick) — MSAL
        # itself also caches the token internally across calls within a
        # process, so this isn't strictly required, but avoids re-auth
        # chatter within a single fetch/send cycle.
        if self._token:
            return self._token
        import msal

        app = msal.ConfidentialClientApplication(
            self.client_id,
            authority=f"https://login.microsoftonline.com/{self.tenant_id}",
            client_credential=self.client_secret,
        )
        result = app.acquire_token_for_client(scopes=["https://graph.microsoft.com/.default"])
        if "access_token" not in result:
            raise RuntimeError(
                f"Graph auth failed: {result.get('error')} — {result.get('error_description')}"
            )
        self._token = result["access_token"]
        return self._token

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._access_token()}", "Content-Type": "application/json"}

    def fetch_new_messages(self):
        import requests

        url = (
            f"{_GRAPH_BASE}/users/{self.mailbox}/mailFolders/Inbox/messages"
            "?$filter=isRead eq false"
            "&$select=id,subject,from,ccRecipients,internetMessageId,hasAttachments"
            "&$top=50"
        )
        resp = requests.get(url, headers=self._headers(), timeout=30)
        resp.raise_for_status()
        graph_messages = resp.json().get("value", [])

        messages = []
        for gm in graph_messages:
            if not gm.get("hasAttachments"):
                self._mark_read(gm["id"])
                continue

            pdf_attachments = self._fetch_pdf_attachments(gm["id"])
            if not pdf_attachments:
                # Not an invoice we can act on (no PDF) — mark read so it
                # doesn't get re-checked on every poll from now on.
                self._mark_read(gm["id"])
                continue

            sender = ((gm.get("from") or {}).get("emailAddress") or {}).get("address", "")
            cc = ", ".join(
                (r.get("emailAddress") or {}).get("address", "")
                for r in gm.get("ccRecipients", [])
            )
            for att in pdf_attachments:
                messages.append({
                    "filename": att["name"],
                    "data": base64.b64decode(att["contentBytes"]),
                    "sender_email": sender,
                    "subject": gm.get("subject") or "",
                    "cc_emails": cc,
                    "message_id": gm.get("internetMessageId") or gm["id"],
                })
            self._mark_read(gm["id"])
        return messages

    def _fetch_pdf_attachments(self, message_id: str) -> list:
        import requests

        url = f"{_GRAPH_BASE}/users/{self.mailbox}/messages/{message_id}/attachments"
        resp = requests.get(url, headers=self._headers(), timeout=30)
        resp.raise_for_status()
        attachments = resp.json().get("value", [])
        return [
            a for a in attachments
            if a.get("@odata.type") == "#microsoft.graph.fileAttachment"
            and (a.get("contentType") == "application/pdf" or a.get("name", "").lower().endswith(".pdf"))
        ]

    def _mark_read(self, message_id: str):
        import requests

        url = f"{_GRAPH_BASE}/users/{self.mailbox}/messages/{message_id}"
        requests.patch(url, headers=self._headers(), json={"isRead": True}, timeout=30)

    def send_email(self, to_email, subject, body, invoice_id=None, in_reply_to=None, attachment=None):
        import requests

        message = {
            "subject": subject,
            "body": {"contentType": "Text", "content": body},
            "toRecipients": [{"emailAddress": {"address": to_email}}],
        }
        if attachment:
            message["attachments"] = [{
                "@odata.type": "#microsoft.graph.fileAttachment",
                "name": attachment["filename"],
                "contentType": attachment.get("mimetype", "application/pdf"),
                "contentBytes": base64.b64encode(attachment["data"]).decode("ascii"),
            }]

        url = f"{_GRAPH_BASE}/users/{self.mailbox}/sendMail"
        resp = requests.post(url, headers=self._headers(), json={"message": message, "saveToSentItems": True}, timeout=30)
        resp.raise_for_status()

        # Still logged locally for the in-app Outbox view, same as mock mode.
        log = OutgoingEmailLog(to_email=to_email, subject=subject, body=body, invoice_id=invoice_id)
        if attachment:
            log.attachment_filename = attachment["filename"]
            log.attachment_data = attachment["data"]
            log.attachment_mimetype = attachment.get("mimetype", "application/pdf")
        db.session.add(log)
        db.session.commit()


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
