"""
Item 1 — Pre-screen emails before they touch Claude.
Catches auto-replies, out-of-office, delivery failures, and newsletters.
"""
import re

_FROM = re.compile(
    r"(noreply|no-reply|do[\-.]?not[\-.]?reply|donotreply|mailer-daemon|"
    r"postmaster|bounces?@|automated@|notifications?@|alerts?@)",
    re.IGNORECASE,
)

_SUBJECT = re.compile(
    r"^(re:\s*)?(out of office|automatic reply|auto[\-\s]?reply|"
    r"delivery status notification|undeliverable|mail delivery failed|"
    r"delivery failure|read receipt|"
    r"newsletter|unsubscribe|\[spam\])",
    re.IGNORECASE,
)

_BODY_FIRST300 = re.compile(
    r"(this is an auto(matic|mated)[\s\-]?(reply|response|message)|"
    r"do not reply to this (email|message)|"
    r"this message was sent automatically|"
    r"you are receiving this (email|message) because)",
    re.IGNORECASE,
)


def is_spam(from_addr: str, subject: str, body: str) -> bool:
    if _FROM.search(from_addr):
        return True
    if _SUBJECT.search(subject.strip()):
        return True
    if _BODY_FIRST300.search(body[:300]):
        return True
    return False
