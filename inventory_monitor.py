"""
inventory_monitor.py — Check Biogents product availability and auto-reorder when stock is low.

Reads per-SKU thresholds from .env:
  BIOGENTS_SKUS=bg-pro-trap,bg-mosquitaire          (comma-separated SKU identifiers)
  BIOGENTS_REORDER_THRESHOLD_<SKU>=5                (reorder when stock <= this)
  BIOGENTS_REORDER_QTY_<SKU>=10                     (how many to order)
  BIOGENTS_WAREHOUSE_ADDRESS=...                     (ship-to for warehouse restocks)

Run daily at 8am CT via launchd (com.greenguard.inventorymonitor.plist).
Also runnable manually:  python inventory_monitor.py
"""

import asyncio
import logging
import os
import re
import sys

from dotenv import load_dotenv

load_dotenv()

import db
import sms_client
from biogentspro_client import get_products, place_order, run_with_browser

log = logging.getLogger(__name__)

BIOGENTS_SKUS_RAW      = os.getenv("BIOGENTS_SKUS", "")
WAREHOUSE_ADDRESS      = os.getenv("BIOGENTS_WAREHOUSE_ADDRESS", os.getenv("DEPOT_ADDRESS", ""))
WAREHOUSE_CITY         = os.getenv("BIOGENTS_WAREHOUSE_CITY", "Austin")
WAREHOUSE_STATE        = os.getenv("BIOGENTS_WAREHOUSE_STATE", "TX")
WAREHOUSE_ZIP          = os.getenv("BIOGENTS_WAREHOUSE_ZIP", "78703")


def _sku_env_key(sku: str) -> str:
    """Convert 'bg-pro-trap' → 'BG_PRO_TRAP' for env var lookups."""
    return re.sub(r"[^A-Z0-9]", "_", sku.upper())


def _threshold(sku: str) -> int:
    key = f"BIOGENTS_REORDER_THRESHOLD_{_sku_env_key(sku)}"
    return int(os.getenv(key, os.getenv("BIOGENTS_REORDER_THRESHOLD", "3")))


def _reorder_qty(sku: str) -> int:
    key = f"BIOGENTS_REORDER_QTY_{_sku_env_key(sku)}"
    return int(os.getenv(key, os.getenv("BIOGENTS_REORDER_QTY", "5")))


async def check_and_reorder() -> list[dict]:
    """
    Fetch current product availability from biogentspro.com.
    For each tracked SKU that is out of stock or below threshold, place a restock order.
    Returns a list of action dicts for logging/SMS.
    """
    tracked_skus = [s.strip().lower() for s in BIOGENTS_SKUS_RAW.split(",") if s.strip()]
    if not tracked_skus:
        log.warning("BIOGENTS_SKUS not configured — nothing to monitor")
        return []

    actions = []

    async def _run(page):
        from biogentspro_client import get_products

        products = await get_products(page)
        log.info("Fetched %d products from biogentspro.com", len(products))

        for product in products:
            sku_lower = product.sku.lower()
            name_lower = product.name.lower()

            # Match against any tracked SKU substring
            matched_sku = None
            for tsku in tracked_skus:
                if tsku in sku_lower or tsku in name_lower:
                    matched_sku = tsku
                    break

            if not matched_sku:
                continue

            threshold = _threshold(matched_sku)
            qty_to_order = _reorder_qty(matched_sku)
            stock = product.stock_qty if product.stock_qty is not None else (0 if not product.in_stock else threshold + 1)

            log.info(
                "Product: %s (sku=%s) in_stock=%s stock_qty=%s threshold=%d",
                product.name, product.sku, product.in_stock, product.stock_qty, threshold,
            )

            needs_reorder = not product.in_stock or stock <= threshold
            if not needs_reorder:
                actions.append({"sku": matched_sku, "action": "ok", "stock": stock})
                continue

            log.info("Reordering %d x %s (stock=%s <= threshold=%d)", qty_to_order, matched_sku, stock, threshold)

            try:
                biogents_order_id = await place_order(
                    page,
                    sku=product.sku,
                    quantity=qty_to_order,
                    ship_to_name="Greenguard USA",
                    ship_to_address=WAREHOUSE_ADDRESS,
                    ship_to_city=WAREHOUSE_CITY,
                    ship_to_state=WAREHOUSE_STATE,
                    ship_to_zip=WAREHOUSE_ZIP,
                )

                db.record_equipment_order(
                    biogents_order_id=biogents_order_id or f"restock-{matched_sku}-{int(__import__('time').time())}",
                    customer_email="",
                    customer_name="Greenguard USA (restock)",
                    sku=matched_sku,
                    quantity=qty_to_order,
                    ship_to_address=f"{WAREHOUSE_ADDRESS}, {WAREHOUSE_CITY}, {WAREHOUSE_STATE} {WAREHOUSE_ZIP}",
                )

                actions.append({
                    "sku": matched_sku,
                    "action": "ordered",
                    "qty": qty_to_order,
                    "biogents_order_id": biogents_order_id,
                    "stock": stock,
                })
                log.info("Restock order placed: Biogents #%s — %dx %s", biogents_order_id, qty_to_order, matched_sku)

            except Exception as exc:
                log.error("Failed to reorder %s: %s", matched_sku, exc, exc_info=True)
                actions.append({"sku": matched_sku, "action": "error", "error": str(exc)})

        return actions

    return await run_with_browser(_run)


def _send_summary_sms(actions: list[dict]) -> None:
    if not sms_client.ADMIN_SMS or not actions:
        return

    ordered = [a for a in actions if a["action"] == "ordered"]
    errors  = [a for a in actions if a["action"] == "error"]
    ok      = [a for a in actions if a["action"] == "ok"]

    if not ordered and not errors:
        return  # Nothing interesting to report

    lines = ["Biogents inventory check:"]
    for a in ordered:
        lines.append(f"  Reordered {a['qty']}x {a['sku']} (#{a.get('biogents_order_id', 'pending')})")
    for a in errors:
        lines.append(f"  ERROR reordering {a['sku']}: {a.get('error', '')[:60]}")
    if ok:
        lines.append(f"  {len(ok)} SKU(s) in stock, no action needed")

    sms_client.send_sms(sms_client.ADMIN_SMS, "\n".join(lines))


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    if not os.getenv("BIOGENTS_EMAIL") or not os.getenv("BIOGENTS_PASSWORD"):
        log.error("BIOGENTS_EMAIL and BIOGENTS_PASSWORD must be set in .env")
        sys.exit(1)

    db.init_db()

    log.info("Starting Biogents inventory check...")
    try:
        actions = asyncio.run(check_and_reorder())
        log.info("Inventory check complete: %d SKU(s) evaluated", len(actions))
        _send_summary_sms(actions)
        for a in actions:
            if a["action"] == "ordered":
                log.info("  Ordered: %dx %s → Biogents #%s", a["qty"], a["sku"], a.get("biogents_order_id", "?"))
            elif a["action"] == "error":
                log.error("  Error: %s — %s", a["sku"], a.get("error", ""))
            else:
                log.info("  OK: %s (stock OK)", a["sku"])
    except Exception as exc:
        log.error("Inventory monitor failed: %s", exc, exc_info=True)
        if sms_client.ADMIN_SMS:
            sms_client.send_sms(sms_client.ADMIN_SMS, f"Biogents inventory monitor crashed: {exc}")
        sys.exit(1)


if __name__ == "__main__":
    main()
