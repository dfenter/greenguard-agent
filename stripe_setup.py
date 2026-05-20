"""
One-time script — create Stripe products and prices from the SKU catalog.
Saves stripe_prices.json mapping {SKU_CODE: stripe_price_id}.

Safe to re-run — skips products that already exist (matched by metadata.sku).

Usage:
    python3 stripe_setup.py
"""

import json
import os
import sys
from pathlib import Path

import stripe
from dotenv import load_dotenv

import sku_engine

load_dotenv()

stripe.api_key = os.getenv("STRIPE_SECRET_KEY", "")
PRICES_FILE = Path(__file__).parent / "stripe_prices.json"


def _sku_from_product(product) -> str | None:
    try:
        return product.metadata.to_dict().get("sku")
    except Exception:
        return None


def find_existing_price(sku_code: str, needed_interval: str | None) -> str | None:
    """
    Search all products with this SKU and return a price_id that matches
    the needed interval ('month' for recurring, None for one_time).
    Returns None if no matching price found anywhere.
    """
    for product in stripe.Product.list(limit=100, active=True).auto_paging_iter():
        if _sku_from_product(product) != sku_code:
            continue
        for price in stripe.Price.list(product=product.id, active=True).auto_paging_iter():
            try:
                interval = price.recurring.interval if price.recurring else None
            except Exception:
                interval = None
            if interval == needed_interval:
                return price.id
    return None


def main():
    if not stripe.api_key:
        print("Error: STRIPE_SECRET_KEY not set in .env")
        sys.exit(1)

    mode = "TEST" if stripe.api_key.startswith("sk_test_") else "LIVE"
    print(f"\nStripe Setup — {mode} mode")
    print(f"{'='*50}\n")

    prices_map: dict[str, str] = {}

    if PRICES_FILE.exists():
        try:
            prices_map = json.loads(PRICES_FILE.read_text())
        except Exception:
            pass

    created = skipped = failed = 0

    for sku in sku_engine.all_skus() + sku_engine.all_addons():
        needed_interval = "month" if sku.billing_type == "recurring" else None

        # Check if any existing product+price combo already matches
        existing_price_id = find_existing_price(sku.code, needed_interval)
        if existing_price_id:
            prices_map[sku.code] = existing_price_id
            print(f"  SKIP  {sku.code:<14} {sku.label}")
            skipped += 1
            continue

        try:
            # Create product
            product = stripe.Product.create(
                name=sku.label,
                metadata={"sku": sku.code},
            )

            # Create price
            price_params: dict = {
                "product": product.id,
                "unit_amount": sku.price_cents,
                "currency": "usd",
            }
            if sku.billing_type == "recurring":
                price_params["recurring"] = {"interval": "month"}

            price = stripe.Price.create(**price_params)
            prices_map[sku.code] = price.id

            billing = "monthly" if sku.billing_type == "recurring" else "one-time"
            amount = f"${sku.price_cents / 100:.2f}" if sku.price_cents else "free"
            print(f"  ✓     {sku.code:<14} {sku.label}  ({amount}, {billing})")
            created += 1

        except Exception as e:
            print(f"  ✗     {sku.code:<14} {sku.label}  — {e}")
            failed += 1

    PRICES_FILE.write_text(json.dumps(prices_map, indent=2))
    print(f"\n  Saved {len(prices_map)} price IDs → stripe_prices.json")
    print(f"\nDone: {created} created, {skipped} skipped, {failed} failed.\n")


if __name__ == "__main__":
    main()
