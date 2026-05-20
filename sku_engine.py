"""
SKU engine — maps Cal.com event type slug to SKU code, price, and billing type.
"""

from dataclasses import dataclass

@dataclass
class SKU:
    code: str
    price_cents: int
    billing_type: str   # "recurring" | "one_time"
    label: str


_CATALOG: dict[str, SKU] = {
    "tank-exchange-1":        SKU("TANK1",    8998,  "one_time",  "CO2 Tank Exchange — 1 Tank"),
    "tank-exchange-2":        SKU("TANK2",    13997, "one_time",  "CO2 Tank Exchange — 2 Tanks"),
    "tank-exchange-3":        SKU("TANK3",    18996, "one_time",  "CO2 Tank Exchange — 3 Tanks"),
    "tank-exchange-4":        SKU("TANK4",    23995, "one_time",  "CO2 Tank Exchange — 4 Tanks"),
    "tank-exchange-10":       SKU("TANK10",   53989, "one_time",  "CO2 Tank Exchange — 10 Tanks"),
    "biogents-co2-1":         SKU("BG1",      15999, "recurring", "Biogents CO₂ Service — 1 Trap"),
    "biogents-co2-2":         SKU("BG2",      26699, "recurring", "Biogents CO₂ Service — 2 Traps"),
    "biogents-co2-3":         SKU("BG3",      39999, "recurring", "Biogents CO₂ Service — 3 Traps"),
    "mosqitter-rental":       SKU("MQ-RENT",  29999, "recurring", "Mosqitter Grand — Monthly Rental"),
    "mosqitter-service":      SKU("MQ-SVC",   12999, "recurring", "Mosqitter Grand — Monthly Service"),
    "mosqitter-installation": SKU("MQ-INST",  19999, "one_time",  "Mosqitter Grand — Installation"),
    "mosqitter-troubleshoot": SKU("MQ-TSHOOT",7999,  "one_time",  "Mosqitter Grand — Troubleshooting"),
    "property-assessment":    SKU("ASSESS",   0,     "one_time",  "Free Property Assessment"),
    "tank-refill-check":      SKU("CHK",      0,     "one_time",  "Tank Refill Check"),
    "barrier-treatment":      SKU("BARRIER",  4999,  "one_time",  "GreenGuard Barrier Treatment"),
}


def resolve(slug: str) -> SKU | None:
    """Return SKU for a Cal.com event type slug, or None if unrecognized."""
    return _CATALOG.get(slug)


def all_skus() -> list[SKU]:
    return list(_CATALOG.values())


# ── Add-on catalog (admin-only, not Cal.com event types) ─────────────────────

_ADDONS: dict[str, SKU] = {
    "BAIT":          SKU("BAIT",          1000,  "one_time", "Mosquito Bait Pack"),
    "BG-SWEETSCENT": SKU("BG-SWEETSCENT", 1000,  "one_time", "BG SweetScent Lure"),
    "CO2-ADDON":     SKU("CO2-ADDON",     4999,  "one_time", "Extra CO₂ Tank Add-On"),
    "TRAP-INSTALL":  SKU("TRAP-INSTALL",  8000,  "one_time", "Extra Trap Installation"),
    "TRAP-MAINT-BG": SKU("TRAP-MAINT-BG", 1000,  "one_time", "Tank Hookup + Trap Maintenance — Biogents (per trap)"),
    "TRAP-MAINT-MQ": SKU("TRAP-MAINT-MQ", 3000,  "one_time", "Tank Hookup + Trap Maintenance — Mosqitter"),
    "TIMER-INSTALL": SKU("TIMER-INSTALL", 2999,  "one_time", "Timer Installation"),
    "NONCО2-UNIT":   SKU("NONCО2-UNIT",  7999,  "one_time", "Non-CO₂ Biogents Unit"),
    "WKD-SURCH":     SKU("WKD-SURCH",    2500,  "one_time", "Weekend Service Surcharge"),
    "TANK-RENTAL":   SKU("TANK-RENTAL",  4999,  "one_time", "CO₂ Tank — Rental/Replacement"),
}


def get_addon(code: str) -> SKU | None:
    return _ADDONS.get(code)


def all_addons() -> list[SKU]:
    return list(_ADDONS.values())
