"""
Fetches Gmail canned response templates via the Settings API.
Requires scope: https://www.googleapis.com/auth/gmail.settings.basic
"""

import html
import re


def _strip_html(text: str) -> str:
    text = html.unescape(text)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<p[^>]*>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</p>", "", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    # Collapse excess blank lines
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def get_all_templates(service) -> dict[str, str]:
    """Return {template_name: plain_text_body} for every Gmail canned response."""
    listing = service.users().settings().canned_messages().list(userId="me").execute()
    templates: dict[str, str] = {}

    for entry in listing.get("canned_messages", []):
        tmpl_id = entry["id"]
        name = entry.get("name", tmpl_id)
        try:
            detail = (
                service.users()
                .settings()
                .canned_messages()
                .get(userId="me", id=tmpl_id)
                .execute()
            )
            raw_body = detail.get("body", "")
            templates[name] = _strip_html(raw_body) if raw_body else ""
        except Exception:
            templates[name] = ""

    return templates
