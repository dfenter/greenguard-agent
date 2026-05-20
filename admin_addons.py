"""
admin_addons.py — Manage per-customer default add-ons.

Add-ons listed here are automatically added to every invoice for that customer
when the daily billing run fires.

Usage:
    python3 admin_addons.py                          # list all
    python3 admin_addons.py --email joe@x.com        # show one customer
    python3 admin_addons.py --add --email joe@x.com --sku BAIT
    python3 admin_addons.py --remove --email joe@x.com --sku BAIT
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
    print(f"  {'─'*56}")
    for email, skus in sorted(data.items()):
        labels = ", ".join(
            ADDON_SKUS[s].label if s in ADDON_SKUS else s for s in skus
        )
        print(f"  {email:<36} {labels}")
    print()


def show(email: str):
    data  = _load()
    email = email.lower()
    skus  = data.get(email, [])
    if not skus:
        print(f"  No add-ons configured for {email}")
        return
    print(f"\n  Default add-ons for {email}:")
    for s in skus:
        label = ADDON_SKUS[s].label if s in ADDON_SKUS else s
        price = f"${ADDON_SKUS[s].price_cents/100:.2f}" if s in ADDON_SKUS else ""
        print(f"    • {s:<16} {label}  {price}")
    print()


def add(email: str, sku: str):
    sku   = sku.upper()
    email = email.lower()
    if sku not in ADDON_SKUS:
        valid = ", ".join(ADDON_SKUS.keys())
        print(f"  Unknown SKU '{sku}'. Valid: {valid}")
        sys.exit(1)
    data = _load()
    current = data.get(email, [])
    if sku in current:
        print(f"  {sku} already set for {email}")
        return
    current.append(sku)
    data[email] = current
    _save(data)
    print(f"  ✓ Added {sku} ({ADDON_SKUS[sku].label}) → {email}")


def remove(email: str, sku: str):
    sku   = sku.upper()
    email = email.lower()
    data  = _load()
    current = data.get(email, [])
    if sku not in current:
        print(f"  {sku} not found for {email}")
        return
    current.remove(sku)
    if current:
        data[email] = current
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

    if "--add" in args:
        if not email or not sku:
            print("Usage: --add --email <email> --sku <SKU>")
            sys.exit(1)
        add(email, sku)
    elif "--remove" in args:
        if not email or not sku:
            print("Usage: --remove --email <email> --sku <SKU>")
            sys.exit(1)
        remove(email, sku)
    elif email:
        show(email)
    else:
        list_all()
