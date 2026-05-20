"""
stripe_cleanup.py — Archive all products and prices in the current Stripe account.

Run this against your OLD Stripe account before migrating to the new one.
Archiving is safe: past invoices and subscriptions are unaffected.

Usage:
    python3 stripe_cleanup.py            # dry run — lists what would be archived
    python3 stripe_cleanup.py --execute  # archives everything
"""

import os
import sys

import stripe
from dotenv import load_dotenv

load_dotenv()
stripe.api_key = os.getenv("STRIPE_SECRET_KEY", "")


def run(dry_run: bool = True):
    if not stripe.api_key:
        print("Error: STRIPE_SECRET_KEY not set in .env")
        sys.exit(1)

    mode = "DRY RUN" if dry_run else "LIVE — archiving now"
    acct = "TEST" if stripe.api_key.startswith("sk_test_") else "LIVE"
    print(f"\n{'='*54}")
    print(f"  Stripe Cleanup  |  {acct} account  |  {mode}")
    print(f"{'='*54}\n")

    archived = skipped = 0

    for product in stripe.Product.list(active=True, limit=100).auto_paging_iter():
        name = product.name or product.id
        if dry_run:
            print(f"  ~  {name}")
            archived += 1
        else:
            try:
                # Archive active prices first
                for price in stripe.Price.list(product=product.id, active=True).auto_paging_iter():
                    stripe.Price.modify(price.id, active=False)
                # Archive the product
                stripe.Product.modify(product.id, active=False)
                print(f"  ✓  {name}")
                archived += 1
            except stripe.StripeError as e:
                print(f"  ✗  {name}  — {e}")
                skipped += 1

    print(f"\n  {'Would archive' if dry_run else 'Archived'}: {archived}")
    if skipped:
        print(f"  Failed:         {skipped}")
    if dry_run:
        print(f"\n  Run with --execute to archive {archived} product(s).")
    print(f"\n{'='*54}\n")


if __name__ == "__main__":
    execute = "--execute" in sys.argv
    run(dry_run=not execute)
