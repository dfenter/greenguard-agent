"""
Parses Cal.com "Free Property Assessment between GreenGuard USA and ..." booking emails.
Also handles legacy Acuity "New Appointment: ..." format as a fallback.
"""

import re
from dataclasses import dataclass

# Cal.com subject: "Free Property Assessment between GreenGuard USA and Jane Smith"
_CALCOM_SUBJECT = re.compile(
    r"Free Property Assessment between GreenGuard USA",
    re.IGNORECASE,
)


@dataclass
class AppointmentInfo:
    customer_name: str
    customer_email: str
    customer_phone: str
    service_type: str
    service_date: str
    service_address: str
    customer_notes: str
    raw_subject: str


def is_appointment_notification(subject: str) -> bool:
    """Return True if this looks like a Cal.com or Acuity booking notification."""
    return bool(
        _CALCOM_SUBJECT.search(subject)
        or re.search(r"New Appointment:", subject, re.IGNORECASE)
    )


def parse_appointment_email(subject: str, body: str) -> AppointmentInfo:
    if _CALCOM_SUBJECT.search(subject):
        return _parse_calcom(subject, body)
    return _parse_acuity(subject, body)


# ---------------------------------------------------------------------------
# Cal.com parser
# ---------------------------------------------------------------------------

def _parse_calcom(subject: str, body: str) -> AppointmentInfo:
    # Customer name from subject: "... between GreenGuard USA and Jane Smith"
    name_m = re.search(r"between GreenGuard USA and\s+(.+)$", subject, re.IGNORECASE)
    customer_name = name_m.group(1).strip() if name_m else ""

    def _field(pattern: str) -> str:
        m = re.search(pattern, body, re.IGNORECASE | re.MULTILINE)
        return m.group(1).strip() if m else ""

    # When: "Thursday, May 21, 2026 at 10:00 AM (CST)"  or  "May 21, 2026, 10:00 AM"
    service_date = _field(r"When:\s*([^\n]+)")

    # Who block — grab second attendee's email (first is Greenguard)
    # Cal.com format: "  Jane Smith - jane@example.com"
    customer_email = ""
    who_m = re.search(r"Who:\s*\n((?:[^\n]+\n){1,5})", body, re.IGNORECASE)
    if who_m:
        for line in who_m.group(1).splitlines():
            if "greenguard" not in line.lower():
                em = re.search(r"[\w.+-]+@[\w.-]+\.\w+", line)
                if em:
                    customer_email = em.group(0)
                    if not customer_name:
                        nm = re.match(r"\s*(.+?)\s*-\s*[\w.+-]+@", line)
                        customer_name = nm.group(1).strip() if nm else ""
                    break

    # Fallback: email anywhere in body (not greenguard domain)
    if not customer_email:
        for em in re.finditer(r"[\w.+-]+@[\w.-]+\.\w+", body):
            if "greenguard" not in em.group(0).lower():
                customer_email = em.group(0)
                break

    customer_phone = _field(r"(?:Phone|Mobile|Tel)(?:\s+Number)?:\s*([^\n]+)")

    # Address: Cal.com "Where:" line, then common label fallbacks
    address = _field(r"Where:\s*([^\n]+)")
    if not address or not re.search(r"\d", address):
        address = _field(r"(?:Service\s+)?(?:Address|Location):\s*([^\n]+)")
    if not address or not re.search(r"\d", address):
        address = _field(r"(?:Property\s+)?Address:\s*([^\n]+)")

    # Additional questions / notes block
    notes_m = re.search(
        r"(?:Additional\s+)?(?:Notes?|Comments?|Message|Info):\s*(.+?)(?:\n\n|\Z)",
        body,
        re.DOTALL | re.IGNORECASE,
    )
    customer_notes = notes_m.group(1).strip() if notes_m else ""

    return AppointmentInfo(
        customer_name=customer_name,
        customer_email=customer_email,
        customer_phone=customer_phone,
        service_type="Free Property Assessment",
        service_date=service_date,
        service_address=address,
        customer_notes=customer_notes,
        raw_subject=subject,
    )


# ---------------------------------------------------------------------------
# Legacy Acuity parser (kept as fallback)
# ---------------------------------------------------------------------------

def _parse_acuity(subject: str, body: str) -> AppointmentInfo:
    name_m = re.search(r"New Appointment:.*?\bwith\s+(.+)$", subject, re.IGNORECASE)
    customer_name = name_m.group(1).strip() if name_m else ""

    svc_m = re.search(r"New Appointment:\s*(.+?)\s+with\b", subject, re.IGNORECASE)
    service_type = svc_m.group(1).strip() if svc_m else "CO2 Mosquito Control"

    def _field(pattern: str) -> str:
        m = re.search(pattern, body, re.IGNORECASE)
        return m.group(1).strip() if m else ""

    customer_email = _field(r"Email:\s*([^\s\n]+)")
    customer_phone = _field(r"Phone:\s*([^\n]+)")
    service_date = _field(r"(?:Appointment\s+)?Date:\s*([^\n]+)")

    address = ""
    m = re.search(
        r"Address\s*\n[=\-]{3,}\s*\n(?:[^\n]*?:+\s*)?\n?([^\n].+?)(?:\n\n|\Z)",
        body, re.DOTALL | re.IGNORECASE,
    )
    if m:
        candidate = m.group(1).strip().split("\n")[0]
        if re.search(r"\d", candidate):
            address = candidate

    if not address:
        m = re.search(r"Please enter the address[^:\n]*:+\s*([^\n]+)", body, re.IGNORECASE)
        if m:
            candidate = m.group(1).strip()
            if re.search(r"\d", candidate):
                address = candidate

    if not address:
        m = re.search(r"(?:Service\s+)?(?:Address|Location):\s*([^\n]+)", body, re.IGNORECASE)
        if m:
            candidate = m.group(1).strip()
            if re.search(r"\d", candidate):
                address = candidate

    notes_m = re.search(
        r"(?:Notes?|Additional\s+Notes?|Comments?):\s*(.+?)(?:\n\n|\Z)",
        body, re.DOTALL | re.IGNORECASE,
    )
    customer_notes = notes_m.group(1).strip() if notes_m else ""

    return AppointmentInfo(
        customer_name=customer_name,
        customer_email=customer_email,
        customer_phone=customer_phone,
        service_type=service_type,
        service_date=service_date,
        service_address=address,
        customer_notes=customer_notes,
        raw_subject=subject,
    )
