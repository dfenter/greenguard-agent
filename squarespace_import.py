"""
squarespace_import.py — Import Squarespace shop products into Stripe (live account).

Reads a Squarespace product export CSV and creates Stripe products + one-time prices.
Uses sale price when on sale, regular price otherwise.
Never modifies existing Stripe products.

Usage:
    python3 squarespace_import.py                          # dry run
    python3 squarespace_import.py --execute                # live
    python3 squarespace_import.py --csv path/to/file.csv  # custom path
"""

import csv
import json
import os
import re
import sys
from pathlib import Path

import stripe
from dotenv import load_dotenv

load_dotenv()

# Uses STRIPE_LIVE_KEY — separate from test key so both coexist in .env
stripe.api_key = os.getenv("STRIPE_LIVE_KEY", "")

DEFAULT_CSV  = Path.home() / "Downloads" / "products_May-20_08-40-27AM.csv"
OUTPUT_FILE  = Path(__file__).parent / "squarespace_stripe_ids.json"


def _price_cents(row: dict) -> int:
    """Return the effective price in cents — sale price if on sale, else regular."""
    on_sale    = row.get("On Sale", "").strip().lower() == "yes"
    sale_str   = row.get("Sale Price", "0").strip()
    reg_str    = row.get("Price", "0").strip()
    try:
        sale = float(sale_str)
    except ValueError:
        sale = 0.0
    try:
        reg = float(reg_str)
    except ValueError:
        reg = 0.0

    if on_sale and sale > 0:
        return round(sale * 100)
    return round(reg * 100)


def _strip_html(html: str) -> str:
    """Remove HTML tags for Stripe description (plain text only)."""
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:500]  # Stripe description limit


def existing_skus() -> set[str]:
    """Return set of SKUs already in this Stripe account."""
    skus = set()
    for product in stripe.Product.list(limit=100).auto_paging_iter():
        try:
            sku = product.metadata.to_dict().get("squarespace_sku")
        except Exception:
            sku = None
        if sku:
            skus.add(sku)
    return skus


def run(csv_path: Path, dry_run: bool = True):
    if not stripe.api_key:
        print("Error: STRIPE_LIVE_KEY not set in .env")
        sys.exit(1)

    mode = "TEST" if stripe.api_key.startswith("sk_test_") else "LIVE"
    print(f"\n{'='*60}")
    print(f"  Squarespace → Stripe  |  {mode} account  |  {'DRY RUN' if dry_run else 'LIVE'}")
    print(f"{'='*60}\n")

    if mode == "TEST" and not dry_run:
        print("  Warning: STRIPE_LIVE_KEY appears to be a test key. Proceeding anyway.\n")

    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))

    print(f"  {len(rows)} products in CSV\n")
    print(f"  Checking existing Stripe products …")
    already = existing_skus() if not dry_run else set()
    print(f"  {len(already)} already imported\n")
    print(f"  {'─'*58}")

    results: dict[str, dict] = {}
    created = skipped = failed = 0

    for row in rows:
        title   = row.get("Title", "").strip()
        sku     = row.get("SKU", "").strip()
        visible = row.get("Visible", "").strip().lower() == "yes"
        desc    = _strip_html(row.get("Description", ""))
        price   = _price_cents(row)
        url     = row.get("Product URL", "").strip()

        if not title or not sku:
            continue

        if sku in already:
            print(f"  SKIP  {title[:50]}")
            skipped += 1
            continue

        vis_str = "visible" if visible else "hidden"
        price_str = f"${price/100:.2f}"

        if dry_run:
            print(f"  ~     {title[:50]:<52} {price_str}  ({vis_str})")
            created += 1
            continue

        try:
            product = stripe.Product.create(
                name=title,
                description=desc or None,
                active=visible,
                metadata={
                    "squarespace_sku": sku,
                    "squarespace_url": url,
                    "source": "squarespace_import",
                },
            )
            price_obj = stripe.Price.create(
                product=product.id,
                unit_amount=price,
                currency="usd",
            )
            results[sku] = {"product_id": product.id, "price_id": price_obj.id, "title": title}
            print(f"  ✓     {title[:50]:<52} {price_str}  ({vis_str})")
            created += 1
        except stripe.StripeError as e:
            print(f"  ✗     {title[:50]}  — {e}")
            failed += 1

    if results:
        OUTPUT_FILE.write_text(json.dumps(results, indent=2))
        print(f"\n  Saved IDs → {OUTPUT_FILE.name}")

    print(f"\n  {'─'*58}")
    verb = "Would create" if dry_run else "Created"
    print(f"  {verb}: {created}  |  Skipped: {skipped}  |  Failed: {failed}")
    if dry_run and created:
        print(f"\n  Run with --execute to create {created} product(s) in Stripe.")
    print(f"\n{'='*60}\n")


if __name__ == "__main__":
    execute  = "--execute" in sys.argv
    dry_run  = not execute
    csv_path = DEFAULT_CSV
    for i, arg in enumerate(sys.argv[1:]):
        if arg == "--csv" and i + 1 < len(sys.argv) - 1:
            csv_path = Path(sys.argv[i + 2])
    if not csv_path.exists():
        print(f"CSV not found: {csv_path}")
        sys.exit(1)
    run(csv_path, dry_run)
