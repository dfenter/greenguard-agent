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


def get_existing_products() -> dict[str, str]:
    """Return {sku_code: product_id} for products already in Stripe."""
    existing = {}
    for product in stripe.Product.list(limit=100).auto_paging_iter():
        try:
            sku = product["metadata"]["sku"]
        except (KeyError, TypeError):
            sku = None
        if sku:
            existing[sku] = product.id
    return existing


def get_existing_prices(product_id: str) -> list[dict]:
    return [
        {"id": p.id, "interval": p.recurring["interval"] if p.recurring else None}
        for p in stripe.Price.list(product=product_id, active=True).data
    ]


def main():
    if not stripe.api_key:
        print("Error: STRIPE_SECRET_KEY not set in .env")
        sys.exit(1)

    mode = "TEST" if stripe.api_key.startswith("sk_test_") else "LIVE"
    print(f"\nStripe Setup — {mode} mode")
    print(f"{'='*50}\n")

    existing_products = get_existing_products()
    prices_map: dict[str, str] = {}

    if PRICES_FILE.exists():
        try:
            prices_map = json.loads(PRICES_FILE.read_text())
        except Exception:
            pass

    created = skipped = failed = 0

    for sku in sku_engine.all_skus() + sku_engine.all_addons():
        # Skip duplicates
        if sku.code in existing_products:
            product_id = existing_products[sku.code]
            existing_prices = get_existing_prices(product_id)
            needed_interval = "month" if sku.billing_type == "recurring" else None

            # Check if a price with the correct interval exists
            matching = [p for p in existing_prices if p["interval"] == needed_interval]
            if matching:
                prices_map[sku.code] = matching[0]["id"]
                print(f"  SKIP  {sku.code:<14} {sku.label}")
                skipped += 1
                continue
            else:
                # Price type mismatch — create a new price with correct interval
                print(f"  FIX   {sku.code:<14} creating {sku.billing_type} price …")

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
