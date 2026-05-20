"""
admin_addons.py — Manage per-customer default add-ons.

Add-ons are auto-added to every invoice at billing time.
Format in customer_addons.json: {"email": {"SKU": quantity}}

Usage:
    python3 admin_addons.py                                   # list all
    python3 admin_addons.py --email joe@x.com                 # show one customer
    python3 admin_addons.py --add --email joe@x.com --sku BAIT --qty 2
    python3 admin_addons.py --remove --email joe@x.com --sku BAIT
    python3 admin_addons.py --skus                            # list available SKUs
"""

import json
import sys
from pathlib import Path

import sku_engine

ADDONS_FILE = Path(__file__).parent / "customer_addons.json"
ADDON_SKUS  = {s.code: s for s in sku_engine.all_addons()}


def _load() -> dict:
    if not ADDONS_FILE.exists():
        return {}
    data = json.loads(ADDONS_FILE.read_text())
    return {k: v for k, v in data.items() if not k.startswith("_")}


def _save(data: dict):
    out = {"_comment": "Default add-ons auto-added to every invoice. Keys are lowercase emails."}
    out.update(data)
    ADDONS_FILE.write_text(json.dumps(out, indent=2))


def list_all():
    data = _load()
    if not data:
        print("  No customer add-ons configured.")
        return
    print(f"\n  {'Email':<36} Add-ons")
    print(f"  {'─'*62}")
    for email, addons in sorted(data.items()):
        parts = [f"{qty}× {s}" for s, qty in addons.items()]
        print(f"  {email:<36} {', '.join(parts)}")
    print()


def show(email: str):
    data  = _load()
    email = email.lower()
    addons = data.get(email, {})
    if not addons:
        print(f"  No add-ons configured for {email}")
        return
    print(f"\n  Default add-ons for {email}:")
    for s, qty in addons.items():
        label = ADDON_SKUS[s].label if s in ADDON_SKUS else s
        price = f"${ADDON_SKUS[s].price_cents/100:.2f} each" if s in ADDON_SKUS else ""
        print(f"    • {qty}× {s:<14} {label}  {price}")
    print()


def add(email: str, sku: str, qty: int = 1):
    sku   = sku.upper()
    email = email.lower()
    if sku not in ADDON_SKUS:
        valid = ", ".join(ADDON_SKUS.keys())
        print(f"  Unknown SKU '{sku}'. Valid: {valid}")
        sys.exit(1)
    data = _load()
    addons = data.get(email, {})
    addons[sku] = qty
    data[email] = addons
    _save(data)
    print(f"  ✓ Set {qty}× {sku} ({ADDON_SKUS[sku].label}) → {email}")


def remove(email: str, sku: str):
    sku   = sku.upper()
    email = email.lower()
    data  = _load()
    addons = data.get(email, {})
    if sku not in addons:
        print(f"  {sku} not found for {email}")
        return
    del addons[sku]
    if addons:
        data[email] = addons
    else:
        data.pop(email, None)
    _save(data)
    print(f"  ✓ Removed {sku} from {email}")


def print_skus():
    print("\n  Available add-on SKUs:\n")
    for s in sku_engine.all_addons():
        print(f"    {s.code:<16} {s.label}  ${s.price_cents/100:.2f}")
    print()


if __name__ == "__main__":
    args = sys.argv[1:]

    if not args:
        list_all()
        sys.exit(0)

    if "--skus" in args:
        print_skus()
        sys.exit(0)

    email = sku = ""
    for i, a in enumerate(args):
        if a == "--email" and i + 1 < len(args): email = args[i+1]
        if a == "--sku"   and i + 1 < len(args): sku   = args[i+1]

    qty = 1
    for i, a in enumerate(args):
        if a == "--qty" and i + 1 < len(args):
            try: qty = int(args[i+1])
            except ValueError: pass

    if "--add" in args:
        if not email or not sku:
            print("Usage: --add --email <email> --sku <SKU> [--qty N]")
            sys.exit(1)
        add(email, sku, qty)
    elif "--remove" in args:
        if not email or not sku:
            print("Usage: --remove --email <email> --sku <SKU>")
            sys.exit(1)
        remove(email, sku)
    elif email:
        show(email)
    else:
        list_all()
