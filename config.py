import os

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


def _normalize_db_url(url: str) -> str:
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    return url


class Config:
    SECRET_KEY = os.environ.get("SECRET_KEY", "dev-only-change-me")

    # A single shared password gates the whole app once it's on the public
    # internet — not per-person accounts, just enough to keep it from being
    # wide open to anyone who finds the URL. Set APP_PASSWORD on Railway;
    # locally it's unset, so the gate is skipped entirely.
    APP_PASSWORD = os.environ.get("APP_PASSWORD")

    _db = os.environ.get("DATABASE_URL")
    SQLALCHEMY_DATABASE_URI = (
        _normalize_db_url(_db) if _db else "sqlite:///pw_invoices.db"
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    MAX_CONTENT_LENGTH = int(os.environ.get("MAX_UPLOAD_MB", "20")) * 1024 * 1024

    # Intake mailbox this whole app is scoped to for the pilot.
    INTAKE_MAILBOX = os.environ.get("INTAKE_MAILBOX", "pw-invoices@berkeleyca.gov")

    # "mock" = read .eml/.pdf files dropped in EMAIL_DROP_DIR, log outgoing
    # emails to the console/DB instead of sending. "graph" = real Microsoft
    # Graph API (requires IT-issued app registration). Start in mock mode.
    EMAIL_MODE = os.environ.get("EMAIL_MODE", "mock")
    EMAIL_DROP_DIR = os.environ.get("EMAIL_DROP_DIR", "email_drop")

    # Microsoft Graph API creds (set once IT approves the pilot).
    GRAPH_TENANT_ID = os.environ.get("GRAPH_TENANT_ID")
    GRAPH_CLIENT_ID = os.environ.get("GRAPH_CLIENT_ID")
    GRAPH_CLIENT_SECRET = os.environ.get("GRAPH_CLIENT_SECRET")

    # Used to build links in outgoing emails (e.g. "review this invoice at
    # ..."). Update once the app has a real hostname (Railway, IT server, etc).
    BASE_URL = os.environ.get("BASE_URL", "http://localhost:5051")

    ORG_NAME = "City of Berkeley — Public Works"
    LOGO_PATH = "static/img/berkeley_logo.png"

    # Days before due date to send a reminder.
    REMINDER_DAYS_BEFORE_DUE = [7, 3, 1]
