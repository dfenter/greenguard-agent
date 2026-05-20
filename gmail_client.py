import base64
import os
import re
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.settings.basic",
    "https://www.googleapis.com/auth/calendar",          # full read/write for rescheduling
    "https://www.googleapis.com/auth/pubsub",
]

_DIR = os.path.dirname(os.path.abspath(__file__))


def _write_env_credential(env_var: str, dest_path: str) -> bool:
    """Decode a base64 env var to a file. Returns True if written."""
    val = os.getenv(env_var, "")
    if not val:
        return False
    with open(dest_path, "w") as f:
        f.write(base64.b64decode(val).decode())
    return True


def authenticate(
    credentials_path: str = None,
    token_path: str = None,
) -> tuple:
    """Return (gmail_service, credentials).

    On Render (or any server), set GOOGLE_CREDENTIALS_JSON and GOOGLE_TOKEN_JSON
    env vars to the base64-encoded contents of credentials.json and token.json.
    Locally, the files are used directly.
    """
    if credentials_path is None:
        credentials_path = os.path.join(_DIR, "credentials.json")
    if token_path is None:
        token_path = os.path.join(_DIR, "token.json")

    # On a remote server the files won't exist — decode from env vars instead
    _write_env_credential("GOOGLE_CREDENTIALS_JSON", credentials_path)
    _write_env_credential("GOOGLE_TOKEN_JSON", token_path)

    creds = None
    if os.path.exists(token_path):
        creds = Credentials.from_authorized_user_file(token_path, SCOPES)
        if creds and creds.scopes and not set(SCOPES).issubset(set(creds.scopes)):
            creds = None
            os.remove(token_path)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
            with open(token_path, "w") as f:
                f.write(creds.to_json())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(credentials_path, SCOPES)
            creds = flow.run_local_server(port=0)
            with open(token_path, "w") as f:
                f.write(creds.to_json())

    gmail_service = build("gmail", "v1", credentials=creds)
    return gmail_service, creds


def get_or_create_label(service, label_name: str) -> str:
    result = service.users().labels().list(userId="me").execute()
    for label in result.get("labels", []):
        if label["name"] == label_name:
            return label["id"]
    created = (
        service.users()
        .labels()
        .create(userId="me", body={"name": label_name, "labelListVisibility": "labelShow", "messageListVisibility": "show"})
        .execute()
    )
    return created["id"]


def _decode_body(payload: dict) -> str:
    """Extract plain text from a Gmail message payload."""
    mime_type = payload.get("mimeType", "")
    body = payload.get("body", {})

    if mime_type == "text/plain" and body.get("data"):
        return base64.urlsafe_b64decode(body["data"]).decode("utf-8", errors="replace")

    if mime_type == "text/html" and body.get("data"):
        html = base64.urlsafe_b64decode(body["data"]).decode("utf-8", errors="replace")
        return re.sub(r"<[^>]+>", "", html)

    # Recurse into multipart
    for part in payload.get("parts", []):
        text = _decode_body(part)
        if text.strip():
            return text

    return ""


def get_unread_emails(service, exclude_label_id: str) -> list[dict]:
    """Return unread emails not yet tagged with the processed label."""
    query = f"is:unread -label:{exclude_label_id}"
    result = (
        service.users()
        .messages()
        .list(userId="me", q=query, maxResults=20)
        .execute()
    )
    messages = result.get("messages", [])

    emails = []
    for msg in messages:
        full = (
            service.users()
            .messages()
            .get(userId="me", id=msg["id"], format="full")
            .execute()
        )
        headers = {h["name"]: h["value"] for h in full["payload"]["headers"]}
        emails.append(
            {
                "id": full["id"],
                "thread_id": full["threadId"],
                "from": headers.get("From", ""),
                "to": headers.get("To", ""),
                "subject": headers.get("Subject", "(no subject)"),
                "message_id": headers.get("Message-ID", ""),
                "references": headers.get("References", ""),
                "body": _decode_body(full["payload"]),
            }
        )
    return emails


def create_draft_reply(
    service,
    original: dict,
    draft_subject: str,
    draft_body: str,
    sender_email: str = "admin@greenguard-usa.com",
) -> str:
    """Create a Gmail draft threaded as a reply to original."""
    to_addr = original["from"]
    # Strip display name, keep just address for reply-to
    match = re.search(r"<([^>]+)>", to_addr)
    to_clean = match.group(1) if match else to_addr

    msg = MIMEMultipart("alternative")
    msg["To"] = to_clean
    msg["From"] = sender_email
    msg["Subject"] = draft_subject
    msg["In-Reply-To"] = original["message_id"]
    refs = original["references"]
    msg["References"] = f"{refs} {original['message_id']}".strip()

    msg.attach(MIMEText(draft_body, "plain"))

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    draft = (
        service.users()
        .drafts()
        .create(
            userId="me",
            body={"message": {"raw": raw, "threadId": original["thread_id"]}},
        )
        .execute()
    )
    return draft["id"]


def mark_processed(service, message_id: str, label_ids: list[str]) -> None:
    """Apply one or more labels and mark as read."""
    service.users().messages().modify(
        userId="me",
        id=message_id,
        body={"addLabelIds": label_ids, "removeLabelIds": ["UNREAD"]},
    ).execute()


# ---------------------------------------------------------------------------
# Thread context — pass prior messages to Claude for aware replies
# ---------------------------------------------------------------------------

_THREAD_BODY_LIMIT = 400  # chars per prior message


def get_thread_context(
    service, thread_id: str, current_message_id: str, limit: int = 2
) -> list[dict]:
    """Return up to `limit` prior messages in the thread, oldest first.
    Each entry: {from, date, snippet}
    """
    try:
        thread = (
            service.users()
            .threads()
            .get(userId="me", id=thread_id, format="full")
            .execute()
        )
    except Exception:
        return []

    prior = [m for m in thread.get("messages", []) if m["id"] != current_message_id]
    prior = prior[-limit:]  # keep the most recent N prior messages

    result = []
    for msg in prior:
        headers = {h["name"]: h["value"] for h in msg["payload"]["headers"]}
        body = _decode_body(msg["payload"])
        # Strip quoted content and hard-truncate
        lines = [l for l in body.splitlines() if not l.startswith(">")]
        body = "\n".join(lines).strip()[:_THREAD_BODY_LIMIT]
        result.append({
            "from": headers.get("From", ""),
            "date": headers.get("Date", ""),
            "body": body,
        })
    return result


# ---------------------------------------------------------------------------
# Send a plain-text email (used for the daily digest)
# ---------------------------------------------------------------------------

def send_email(
    service,
    to: str,
    subject: str,
    body: str,
    sender: str = "admin@greenguard-usa.com",
) -> None:
    msg = MIMEText(body, "plain")
    msg["To"] = to
    msg["From"] = sender
    msg["Subject"] = subject
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    service.users().messages().send(
        userId="me", body={"raw": raw}
    ).execute()
