"""
Parses Acuity Scheduling "New Appointment: ..." notification emails.
Extracts customer info, service date, and service address.
"""

import re
from dataclasses import dataclass


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


def parse_appointment_email(subject: str, body: str) -> AppointmentInfo:
    # Subject: "New Appointment: CO2 Mosquito Control with Jane Smith"
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

    # --- Address extraction (tries Acuity's several formats) ---
    address = ""

    # Format 1: Acuity description block with === separator
    m = re.search(
        r"Address\s*\n[=\-]{3,}\s*\n(?:[^\n]*?:+\s*)?\n?([^\n].+?)(?:\n\n|\Z)",
        body,
        re.DOTALL | re.IGNORECASE,
    )
    if m:
        candidate = m.group(1).strip().split("\n")[0]
        if re.search(r"\d", candidate):
            address = candidate

    # Format 2: "Please enter the address..." inline answer
    if not address:
        m = re.search(
            r"Please enter the address[^:\n]*:+\s*([^\n]+)",
            body,
            re.IGNORECASE,
        )
        if m:
            candidate = m.group(1).strip()
            if re.search(r"\d", candidate):
                address = candidate

    # Format 3: generic "Address:" or "Service Address:" or "Location:" label
    if not address:
        m = re.search(
            r"(?:Service\s+)?(?:Address|Location):\s*([^\n]+)",
            body,
            re.IGNORECASE,
        )
        if m:
            candidate = m.group(1).strip()
            if re.search(r"\d", candidate):
                address = candidate

    # --- Notes ---
    notes_m = re.search(
        r"(?:Notes?|Additional\s+Notes?|Comments?):\s*(.+?)(?:\n\n|\Z)",
        body,
        re.DOTALL | re.IGNORECASE,
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
