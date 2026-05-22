"""
stripe_cleanup.py — Archive old Stripe products, keeping new SKUs and Squarespace imports.

Skips any product that:
  - Was created within the last --days (default 7)
  - Has metadata source=squarespace_import (Squarespace sync)
  - Has metadata source=greenguard_sku_setup (new service SKUs)

Usage:
    python3 stripe_cleanup.py                # dry run, 7-day cutoff
    python3 stripe_cleanup.py --days 14      # dry run, 14-day cutoff
    python3 stripe_cleanup.py --execute      # archive old products
"""

import os
import sys
import time
from datetime import datetime, timedelta, timezone

import stripe
from dotenv import load_dotenv

load_dotenv()
stripe.api_key = os.getenv("STRIPE_LIVE_KEY") or os.getenv("STRIPE_SECRET_KEY", "")

PROTECTED_SOURCES = {"squarespace_import", "greenguard_sku_setup"}


def _days_arg() -> int:
    for i, arg in enumerate(sys.argv[1:]):
        if arg == "--days" and i + 1 < len(sys.argv) - 1:
            try:
                return int(sys.argv[i + 2])
            except ValueError:
                pass
    return 7


def run(dry_run: bool = True, days: int = 7):
    if not stripe.api_key:
        print("Error: STRIPE_LIVE_KEY not set in .env")
        sys.exit(1)

    acct      = "TEST" if stripe.api_key.startswith("sk_test_") else "LIVE"
    cutoff    = datetime.now(timezone.utc) - timedelta(days=days)
    cutoff_ts = int(cutoff.timestamp())

    print(f"\n{'='*58}")
    print(f"  Stripe Cleanup  |  {acct}  |  {'DRY RUN' if dry_run else 'ARCHIVING'}")
    print(f"  Archiving products created before {cutoff.strftime('%b %d, %Y')} ({days}d cutoff)")
    print(f"  Protected: squarespace_import, greenguard_sku_setup sources")
    print(f"{'='*58}\n")

    to_archive = skipped_new = skipped_protected = archived = failed = 0

    for product in stripe.Product.list(active=True, limit=100).auto_paging_iter():
        name    = product.name or product.id
        created = product.created  # unix timestamp
        meta    = product.metadata.to_dict() if product.metadata else {}
        source  = meta.get("source", "")

        # Keep products from the new system
        if source in PROTECTED_SOURCES:
            skipped_protected += 1
            continue

        # Keep products created within the cutoff window
        if created >= cutoff_ts:
            skipped_new += 1
            age_days = (time.time() - created) / 86400
            print(f"  KEEP  {name[:52]:<52}  ({age_days:.0f}d old)")
            continue

        age_days = (time.time() - created) / 86400
        if dry_run:
            print(f"  ~     {name[:52]:<52}  ({age_days:.0f}d old)")
            to_archive += 1
        else:
            try:
                for price in stripe.Price.list(product=product.id, active=True).auto_paging_iter():
                    stripe.Price.modify(price.id, active=False)
                stripe.Product.modify(product.id, active=False)
                print(f"  ✓     {name[:52]:<52}  ({age_days:.0f}d old)")
                archived += 1
            except stripe.StripeError as e:
                print(f"  ✗     {name[:52]}  — {e}")
                failed += 1

    print(f"\n  {'─'*54}")
    if dry_run:
        print(f"  Would archive : {to_archive}")
    else:
        print(f"  Archived      : {archived}")
        if failed:
            print(f"  Failed        : {failed}")
    print(f"  Kept (new)    : {skipped_new}")
    print(f"  Kept (system) : {skipped_protected}")
    if dry_run and to_archive:
        print(f"\n  Run with --execute to archive {to_archive} product(s).")
    print(f"\n{'='*58}\n")


if __name__ == "__main__":
    execute = "--execute" in sys.argv
    run(dry_run=not execute, days=_days_arg())
