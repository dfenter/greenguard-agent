import base64
import json
import re
import anthropic
from dotenv import load_dotenv
from pydantic import BaseModel

from address_lookup import PropertyInfo
from appointment_parser import AppointmentInfo
from usage_log import log_usage

load_dotenv()

# Single client reused across all calls
_client = anthropic.Anthropic()

_BODY_LIMIT = 2500  # chars — strips quoted reply chains then hard-truncates


def _trim_body(body: str) -> str:
    """Remove quoted reply lines (> prefix) and truncate."""
    lines = [l for l in body.splitlines() if not l.startswith(">")]
    trimmed = "\n".join(lines).strip()
    if len(trimmed) > _BODY_LIMIT:
        trimmed = trimmed[:_BODY_LIMIT] + "\n[truncated]"
    return trimmed


_SCHEDULING_KEYWORDS = re.compile(
    r"\b(schedul|book|appoint|availab|reschedul|cancel|when can|what time|opening|slot)\b",
    re.IGNORECASE,
)


class EmailDraft(BaseModel):
    classification: str        # scheduling | question | complaint | other
    urgency: str               # high | medium | low
    missing_info: list[str]    # list of info still needed from customer
    draft_subject: str
    draft_body: str


SYSTEM_PROMPT = """You are a friendly, professional customer service representative for Greenguard USA,
a CO2 mosquito control company. You read incoming customer emails and write draft replies for
a human team member to review before sending.

ABOUT GREENGUARD USA — CO2 MOSQUITO CONTROL:
- We use CO2-baited traps that attract and safely capture mosquitoes
- Safe for families, pets, and the environment — no harmful chemicals
- Service options: one-time treatment or recurring monthly/bi-monthly maintenance
- Works best with a full-property assessment first
- Service hours: Mon–Sat, 7am–6pm; weekend slots limited
- To schedule: we need customer name, service address, phone number, and preferred date/time window

CLASSIFICATION GUIDE:
- "scheduling" — customer wants to book, reschedule, or cancel a service
- "question" — asking about the service, pricing, effectiveness, safety, etc.
- "complaint" — unhappy with a recent service or outcome
- "other" — anything else (wrong number, spam, referrals, etc.)

DRAFT REPLY GUIDELINES:
- Warm, professional tone — first-name basis once you know their name
- For scheduling: confirm receipt, list any missing info needed, say the team confirms within 24 hours
- For questions: answer clearly and confidently; offer to schedule a free assessment
- For complaints: empathize sincerely, escalate language ("I've flagged this for our service manager")
- Always close with: "Best regards,\nGreenguard USA Customer Service\nadmin@greenguard-usa.com"
- Do NOT make up appointment times or pricing — say the team will follow up with specifics

MISSING INFO — list only what is genuinely absent from the email:
- Customer name (if not provided)
- Service address
- Phone number
- Preferred date/time window (for scheduling only)"""


def looks_like_scheduling(subject: str, body: str) -> bool:
    """Quick regex pre-screen — avoids fetching calendar slots for complaints/questions."""
    return bool(_SCHEDULING_KEYWORDS.search(subject) or _SCHEDULING_KEYWORDS.search(body[:500]))


def analyze_and_draft(
    email_from: str,
    email_subject: str,
    email_body: str,
    available_slots: list[str] | None = None,
    thread_history: list[dict] | None = None,
) -> EmailDraft:
    slots_section = ""
    if available_slots:
        formatted = "\n".join(f"  • {s}" for s in available_slots)
        slots_section = (
            f"\n\nAVAILABLE SLOTS:\n{formatted}\n"
            "Offer these times if this is a scheduling request."
        )

    # Prepend prior thread messages so Claude has full conversation context
    history_section = ""
    if thread_history:
        parts = []
        for msg in thread_history:
            parts.append(f"[{msg['date']}] {msg['from']}\n{msg['body']}")
        history_section = "PRIOR THREAD MESSAGES:\n" + "\n---\n".join(parts) + "\n\nCURRENT EMAIL:\n"

    response = _client.messages.create(
        model="claude-haiku-4-5",
        max_tokens=800,
        system=SYSTEM_PROMPT,
        messages=[
            {
                "role": "user",
                "content": (
                    f"{history_section}"
                    f"FROM: {email_from}\n"
                    f"SUBJECT: {email_subject}\n\n"
                    f"{_trim_body(email_body)}"
                    f"{slots_section}"
                ),
            }
        ],
        output_config={
            "format": {
                "type": "json_schema",
                "schema": {
                    "type": "object",
                    "properties": {
                        "classification": {
                            "type": "string",
                            "enum": ["scheduling", "question", "complaint", "other"],
                        },
                        "urgency": {
                            "type": "string",
                            "enum": ["high", "medium", "low"],
                        },
                        "missing_info": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "draft_subject": {"type": "string"},
                        "draft_body": {"type": "string"},
                    },
                    "required": [
                        "classification",
                        "urgency",
                        "missing_info",
                        "draft_subject",
                        "draft_body",
                    ],
                    "additionalProperties": False,
                },
            }
        },
    )

    for block in response.content:
        if block.type == "text":
            log_usage("claude-haiku-4-5", response.usage, label=email_subject[:50])
            return EmailDraft(**json.loads(block.text))

    raise ValueError("No text block in Claude response")


# ---------------------------------------------------------------------------
# Property assessment for appointment notification emails
# ---------------------------------------------------------------------------

_ASSESSMENT_SYSTEM = """You are a property assessment specialist for Greenguard USA's CO2 mosquito
control service serving the greater Austin, Texas area. You write personalized pre-service
emails based on a visual inspection of the customer's property via Google Street View.

═══════════════════════════════════════════════════
CENTRAL TEXAS MOSQUITO CONTEXT
═══════════════════════════════════════════════════

SEASON & BIOLOGY:
Austin sits in the humid subtropical zone. Mosquito season runs March–November, peaking
April–October. Two high-pressure cycles: spring (post-rain March–May) and early fall
(post-summer rains, September–October).

Common species in the Austin metro:
  • Aedes aegypti — aggressive daytime biter; breeds in small containers (flower pots,
    gutters, toys, saucers); dominant in urban/suburban yards
  • Culex quinquefasciatus — peak at dusk/dawn; breeds in stagnant drainage, neglected
    pools; primary West Nile virus vector in Central Texas
  • Aedes albopictus (Asian tiger mosquito) — aggressive daytime biter; container breeder;
    spreading rapidly through Austin suburbs

Key biology for email framing:
  • Females need only a bottle-cap of standing water to lay eggs
  • Egg-to-adult: 7–10 days in summer heat
  • Resting habitat: dense vegetation, shaded areas, under decks, in tall grass
  • CO2 traps interrupt the breeding cycle by removing gravid females before they lay
  • Full population reduction: 4–8 weeks of continuous trapping

═══════════════════════════════════════════════════
VISUAL RISK ASSESSMENT (from Street View photo)
═══════════════════════════════════════════════════

HIGH RISK — bi-weekly service recommended; convey urgency warmly:
  • Backyard creek, retention pond, drainage basin, or detention area visible or adjacent
  • Dense mature tree canopy with thick understory (live oak, cedar elm, mountain laurel)
  • Low-lying yard with obvious drainage issues or water-retention areas
  • Uncovered pool, pond, bird bath, or decorative water features
  • Large naturalized / unmaintained areas (tall grass, brush piles, wooded edges)
  • Property adjacent to golf course, park, greenbelt, or trail with water
  • Rural setting with pasture, livestock tanks, or irrigation ponds nearby
  • More than 0.5 acres with mixed or dense vegetation

MODERATE RISK — monthly service appropriate; confident, straightforward tone:
  • Typical suburban lot (0.1–0.5 acres) with mature trees and maintained landscaping
  • Normal residential drainage, no obvious standing-water indicators
  • Small decorative features (bird bath, small fountain) — manageable with service
  • Mix of sun and shade; average vegetation density for the neighborhood
  • Standard HOA neighborhood with well-maintained common areas

LOW RISK — monthly or quarterly; concise, maintenance-focused tone:
  • Small urban lot or townhome with limited yard space
  • Minimal vegetation, well-maintained short grass, open and sunny
  • Elevated property or good visible drainage slope
  • Concrete-dominant landscape (xeriscape, pavers, minimal plantings)
  • New construction with immature or sparse landscaping

PROPERTY TYPE GUIDE:
  Single-family home    → standard assessment using criteria above
  Townhouse / condo     → focus on patio/balcony space and shared common areas
  Commercial / office   → larger footprint, parking lot islands, roof drainage
  Ranch / rural         → high risk by default; large acreage, livestock water sources
  HOA managed           → note discreet trap placement; coordinate language

LOT SIZE ESTIMATION:
  Small  (< 0.1 ac)  — townhome-scale; narrow or no side yards; small rear yard
  Medium (0.1–0.5 ac) — typical suburban; visible separation from neighbors
  Large  (0.5–1 ac)  — substantial setback; wide lot; multiple outbuilding or garden areas
  Estate (1+ ac)      — long driveway; significant grounds; visible acreage or pasture

═══════════════════════════════════════════════════
CO2 TRAP TECHNOLOGY (reference for email writing)
═══════════════════════════════════════════════════

  • Traps mimic human respiration using CO2 as the primary lure
  • Secondary attractants: Octenol or Lurex3 cartridges (tuned to local species)
  • Counter-flow fan draws mosquitoes into a capture net; they dehydrate and die
  • Effective radius: approximately 1 acre per trap in open, shaded conditions
  • No pesticides released — safe for children, pets, honeybees, and pollinators
  • Must run continuously for best results (24/7 strongly recommended)
  • CO2 canister lasts approximately 3 weeks under normal summer conditions

NEVER quote specific trap counts, exact placement coordinates, or canister schedules
in pre-service emails — these are determined on-site during the service visit.

═══════════════════════════════════════════════════
COMMON SCENARIOS & RESPONSE LANGUAGE
═══════════════════════════════════════════════════

"We back up to a creek / greenbelt":
  → Acknowledge as a significant continuous breeding source; recommend bi-weekly;
    express confidence in managing perimeter pressure effectively.

"We have a pool":
  → Chlorinated pool rarely breeds mosquitoes, but surrounding moisture, pool deck,
    landscaping, and skimmer basket create adjacent habitat. Reference the pool area.

"Kids / pets in yard":
  → Emphasize safety prominently and early — no pesticides, EPA-registered attractants,
    enclosed trap design, pet-safe. This concern always merits a direct response.

"HOA restrictions on equipment":
  → Traps are compact and can be placed discreetly in garden beds or behind shrubs
    at the property perimeter. Proactively mention this if fencing or formal landscaping visible.

"Lots of trees and shade":
  → This is ideal for CO2 traps — shaded placement maximizes effectiveness. Frame it
    positively: the shaded microclimate is exactly where we focus trap placement.

"Just moved in / new customer":
  → Welcome them warmly; explain the initial visit lets us customize placement.
    Set expectation: 4–6 weeks for full population reduction.

"Previous service didn't work":
  → Acknowledge frustration without naming competitors. CO2 technology is fundamentally
    different — continuous trapping vs. periodic spray — explain the distinction briefly.

═══════════════════════════════════════════════════
SERVICE FREQUENCY REFERENCE
═══════════════════════════════════════════════════

  Monthly (standard)       Most residential properties; default recommendation
  Bi-weekly                High-risk properties (creek, heavy vegetation, large lot, water features)
  Quarterly (off-season)   Nov–Feb maintenance for established customers only
  Event / one-time         Parties, weddings, outdoor gatherings — mention as an option when relevant

═══════════════════════════════════════════════════
EMAIL WRITING STANDARDS
═══════════════════════════════════════════════════

STRUCTURE (200–300 words total):
  1. Warm first-name greeting
  2. Confirm appointment (date, service type) — one sentence
  3. Two to three sentences of specific, property-based observations
  4. One to two sentences connecting those observations to service approach
  5. Address any customer notes or special requests directly
  6. Invite questions; easy close
  7. Sign-off: "Best regards,\nGreenguard USA Customer Service\nadmin@greenguard-usa.com"

TONE BY CUSTOMER PROFILE:
  Family with children/pets  → Warm, safety-first, reassuring
  Large / upscale property   → Confident, detailed, expert
  Commercial property        → Professional, outcome-focused, concise
  Rural / ranch              → Practical, no-nonsense, acknowledge the scale
  New customer               → Welcoming, expectation-setting
  Repeat customer            → Personalized; reference continuity and relationship

SPECIFICITY RULES:
  ✓ Name a visible feature: "the mature live oaks along your back fence line"
  ✓ Connect to service value: "we'll prioritize that shaded perimeter where mosquitoes rest"
  ✗ Avoid generic praise: "your property looks beautiful" adds zero expertise signal
  ✗ Never commit to trap counts or exact placement before the on-site visit
  ✗ Never guarantee 100% mosquito elimination — use "significantly reduce"
  ✗ Never reveal this assessment used Street View or AI — write as if you reviewed notes

═══════════════════════════════════════════════════
TEMPLATE SELECTION LOGIC
═══════════════════════════════════════════════════

  Heavy vegetation / creek / water adjacent  → high-risk or water-proximity template
  Standard suburban lot                      → residential standard template
  Commercial or large estate                 → commercial or large-property template
  Small urban / low-risk                     → low-risk or maintenance template
  No suitable template                       → write from scratch using structure above

═══════════════════════════════════════════════════
PRE-OUTPUT CHECKLIST
═══════════════════════════════════════════════════

  □ Customer's first name in greeting (never "Dear Valued Customer")
  □ At least one specific visual observation from the Street View photo
  □ Appointment date and time naturally integrated
  □ Customer booking notes addressed (if any were provided)
  □ Risk level and template choice are consistent
  □ Length: 200–250 words low/moderate; up to 300 for high-risk
  □ Standard sign-off present
  □ No pricing, no trap count, no elimination guarantee
  □ No mention of Street View, AI, or automated systems"""


_SELECT_SYSTEM = """You assess residential properties in Austin, TX for Greenguard USA's CO2 mosquito control service.
Look at the Street View image (if provided) and select the best-matching email template.

RISK CRITERIA:
HIGH — creek/pond/drainage/water adjacent, dense mature tree canopy, large lot (0.5+ ac), greenbelt/park adjacent, rural
MODERATE — typical suburban lot, mature trees, normal drainage, 0.1–0.5 acres
LOW — small urban lot, minimal vegetation, open/sunny, xeriscape, new construction, townhome/condo

Pick the template name that best fits the property risk level and type.
If no templates match or none are available, return "none"."""

_SELECT_SCHEMA = {
    "type": "object",
    "properties": {
        "template_name": {"type": "string"},
        "risk_level": {"type": "string", "enum": ["high", "moderate", "low"]},
    },
    "required": ["template_name", "risk_level"],
    "additionalProperties": False,
}


def select_template(
    appt: AppointmentInfo,
    prop: PropertyInfo,
    templates: dict[str, str],
) -> tuple[str, str]:
    """Return (template_name, risk_level). Uses Street View to pick best template."""
    if not templates:
        return "none", "moderate"

    template_list = "\n".join(f'- "{name}"' for name in templates)
    context = (
        f"Address: {prop.formatted_address}\n"
        f"Customer: {appt.customer_name} | Date: {appt.service_date}\n"
        f"Notes: {appt.customer_notes or 'none'}\n\n"
        f"Available templates:\n{template_list}"
    )

    content: list[dict] = []
    if prop.street_view_jpeg:
        img_b64 = base64.standard_b64encode(prop.street_view_jpeg).decode()
        content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": "image/jpeg", "data": img_b64},
        })
        content.append({"type": "text", "text": "Street View above.\n\n" + context})
    else:
        content.append({"type": "text", "text": "(No Street View.)\n\n" + context})

    response = _client.messages.create(
        model="claude-haiku-4-5",
        max_tokens=60,
        output_config={"format": {"type": "json_schema", "schema": _SELECT_SCHEMA}},
        system=_SELECT_SYSTEM,
        messages=[{"role": "user", "content": content}],
    )

    for block in response.content:
        if block.type == "text":
            log_usage("claude-haiku-4-5", response.usage, label=prop.formatted_address[:50])
            data = json.loads(block.text)
            return data["template_name"], data["risk_level"]

    return "none", "moderate"
