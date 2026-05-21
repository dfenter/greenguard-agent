"""
Pre-screen emails before they touch Claude.
Catches auto-replies, marketing, supplier/vendor mail, and notifications
that are never customer emails.
"""
import re

# Sender address patterns — never customer emails
_FROM_PATTERN = re.compile(
    r"(noreply|no-reply|do[\-.]?not[\-.]?reply|donotreply|mailer-daemon|"
    r"postmaster|bounces?@|automated@|notifications?@|alerts?@|"
    r"newsletter@|news@|promo@|offers?@|deals?@|marketing@|"
    r"support@|info@p\.|info@e\.|info@em\.|info@mg\.|"  # bulk ESP subdomains
    r"reply@|digest@|noreply)",
    re.IGNORECASE,
)

# Known non-customer domains (suppliers, banks, delivery, marketing platforms)
_FROM_DOMAINS = re.compile(
    r"@(.*\.)?(biogents\.(com|pro)|"
    r"ups\.com|fedex\.com|usps\.com|dhl\.com|"
    r"chase\.com|bankofamerica\.com|wellsfargo\.com|citi\.com|"
    r"cookunity\.com|doordash\.com|ubereats\.com|grubhub\.com|"
    r"nextdoor\.com|yelp\.com|google\.com|facebook\.com|"
    r"stripe\.com|paypal\.com|quickbooks\.com|"
    r"cal\.com|squarespace\.com|shopify\.com|"
    r"render\.com|github\.com|vercel\.com)",
    re.IGNORECASE,
)

# Subject patterns — marketing, system notifications, non-customer
_SUBJECT = re.compile(
    r"^(re:\s*)?(out of office|automatic reply|auto[\-\s]?reply|"
    r"delivery status notification|undeliverable|mail delivery failed|"
    r"delivery failure|read receipt|newsletter|unsubscribe|\[spam\]|"
    r"your (order|invoice|payment|receipt|shipment|delivery|subscription)|"
    r"order (confirmed|shipped|delivered|#)|invoice #|payment receipt|"
    r"package (delivered|shipped|scheduled)|ups update|tracking|"
    r"\d+% off|\bsale\b|\bdeal\b|\bpromo\b|limited time|"
    r"canceled:|reschedule notice)",
    re.IGNORECASE,
)

# Body patterns — bulk/automated mail
_BODY = re.compile(
    r"(this is an auto(matic|mated)[\s\-]?(reply|response|message)|"
    r"do not reply to this (email|message)|"
    r"this message was sent automatically|"
    r"you are receiving this (email|message) because|"
    r"unsubscribe|manage (your )?(preferences|subscription)|"
    r"view (this )?(email|message) in your browser)",
    re.IGNORECASE,
)


def is_spam(from_addr: str, subject: str, body: str) -> bool:
    if _FROM_PATTERN.search(from_addr):
        return True
    if _FROM_DOMAINS.search(from_addr):
        return True
    if _SUBJECT.search(subject.strip()):
        return True
    if _BODY.search(body[:500]):
        return True
    return False
