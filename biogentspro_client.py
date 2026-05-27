"""
biogentspro_client.py — Playwright browser automation for biogentspro.com

Handles login, order history, inventory checks, and placing orders.
Uses Gemini Vision (via gemini_client.py) for navigation when page layout
requires visual interpretation.

Credentials: BIOGENTS_EMAIL and BIOGENTS_PASSWORD from .env

Standalone test:
    python biogentspro_client.py
"""

import asyncio
import logging
import os
from dataclasses import dataclass, field
from typing import Optional

from dotenv import load_dotenv
from playwright.async_api import Page, async_playwright

load_dotenv()

log = logging.getLogger(__name__)

BIOGENTS_URL      = "https://www.biogentspro.com"
BIOGENTS_EMAIL    = os.getenv("BIOGENTS_EMAIL", "")
BIOGENTS_PASSWORD = os.getenv("BIOGENTS_PASSWORD", "")


@dataclass
class BiogentsOrder:
    order_id: str
    status: str
    tracking_number: str
    carrier: str
    items: list[dict]       # [{sku, name, qty}]
    order_date: str
    ship_to_name: str = ""
    ship_to_address: str = ""


@dataclass
class BiogentsProduct:
    sku: str
    name: str
    price: float
    in_stock: bool
    stock_qty: Optional[int] = None


# ---------------------------------------------------------------------------
# Browser context helpers
# ---------------------------------------------------------------------------

async def _screenshot(page: Page) -> bytes:
    return await page.screenshot(type="png")


async def _gemini_navigate(page: Page, goal: str, max_steps: int = 8) -> bool:
    """Drive the page toward `goal` using Gemini Vision, step by step."""
    from gemini_client import decide_next_action

    for step in range(max_steps):
        screenshot = await _screenshot(page)
        action = decide_next_action(screenshot, goal)
        log.info("Gemini step %d: %s — %s", step + 1, action.get("action"), action.get("reason"))

        act = action.get("action", "error")
        sel = action.get("selector", "")
        val = action.get("value", "")

        if act == "done":
            return True
        if act == "error":
            log.warning("Gemini navigation error: %s", action.get("reason"))
            return False
        if act == "click":
            try:
                await page.click(sel, timeout=5000)
            except Exception:
                # fallback: find by visible text
                await page.get_by_text(sel, exact=False).first.click()
        elif act == "fill":
            await page.fill(sel, val)
        elif act == "select":
            await page.select_option(sel, val)

        await page.wait_for_load_state("networkidle", timeout=10000)

    return False


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------

async def login(page: Page) -> None:
    """Navigate to biogentspro.com and log in. Raises on failure."""
    log.info("Navigating to %s", BIOGENTS_URL)
    await page.goto(BIOGENTS_URL, wait_until="networkidle")

    # Try standard login form selectors first; fall back to Gemini vision
    login_selectors = [
        'a[href*="login"]', 'a[href*="account"]', 'a:has-text("Sign In")',
        'a:has-text("Log In")', 'a:has-text("Account")',
    ]
    clicked = False
    for sel in login_selectors:
        try:
            await page.click(sel, timeout=2000)
            clicked = True
            break
        except Exception:
            continue

    if not clicked:
        log.info("Standard login link not found — using Gemini vision")
        await _gemini_navigate(page, "Click the login or sign in link")

    await page.wait_for_load_state("networkidle")

    # Fill credentials
    email_selectors = ['input[type="email"]', 'input[name*="email"]', 'input[name*="user"]', '#email', '#username']
    pass_selectors  = ['input[type="password"]', 'input[name*="pass"]', '#password']

    filled = False
    for esel in email_selectors:
        try:
            await page.fill(esel, BIOGENTS_EMAIL, timeout=2000)
            filled = True
            break
        except Exception:
            continue

    if not filled:
        log.info("Email field not found — using Gemini vision for login form")
        await _gemini_navigate(page, f"Fill in the email field with {BIOGENTS_EMAIL} and the password field, then submit the login form")
        await page.wait_for_load_state("networkidle")
        log.info("Gemini login complete")
        return

    for psel in pass_selectors:
        try:
            await page.fill(psel, BIOGENTS_PASSWORD, timeout=2000)
            break
        except Exception:
            continue

    submit_selectors = ['button[type="submit"]', 'input[type="submit"]', 'button:has-text("Sign In")', 'button:has-text("Log In")']
    for ssel in submit_selectors:
        try:
            await page.click(ssel, timeout=2000)
            break
        except Exception:
            continue

    await page.wait_for_load_state("networkidle")
    log.info("Login complete (URL: %s)", page.url)


# ---------------------------------------------------------------------------
# Order history
# ---------------------------------------------------------------------------

async def get_orders(page: Page) -> list[BiogentsOrder]:
    """Return recent orders from the account order history page."""
    orders = []

    # Navigate to orders page
    order_page_candidates = [
        f"{BIOGENTS_URL}/account/orders",
        f"{BIOGENTS_URL}/my-account/orders",
        f"{BIOGENTS_URL}/orders",
    ]
    navigated = False
    for url in order_page_candidates:
        try:
            resp = await page.goto(url, wait_until="networkidle", timeout=10000)
            if resp and resp.ok:
                navigated = True
                break
        except Exception:
            continue

    if not navigated:
        await _gemini_navigate(page, "Navigate to order history or my orders page")

    # Parse order rows — try common table/list patterns
    rows = await page.query_selector_all("table tr, .order-row, .woocommerce-orders-table__row, [class*='order']")

    for row in rows:
        try:
            text = await row.inner_text()
            lines = [l.strip() for l in text.splitlines() if l.strip()]
            if not lines or len(lines) < 2:
                continue

            # Extract order ID — look for # prefix or "Order" keyword
            order_id = ""
            for part in lines:
                if part.startswith("#") or "order" in part.lower():
                    order_id = part.lstrip("#").split()[-1]
                    break

            if not order_id:
                continue

            status = ""
            tracking = ""
            carrier = ""
            for part in lines:
                pl = part.lower()
                if any(s in pl for s in ["processing", "shipped", "delivered", "pending", "completed", "cancelled"]):
                    status = part
                if any(c in pl for c in ["1z", "9400", "ups", "usps", "fedex", "dhl"]):
                    tracking = part
                    if "ups" in pl:
                        carrier = "UPS"
                    elif "usps" in pl or "9400" in part:
                        carrier = "USPS"
                    elif "fedex" in pl:
                        carrier = "FedEx"
                    elif "dhl" in pl:
                        carrier = "DHL"

            orders.append(BiogentsOrder(
                order_id=order_id,
                status=status or "unknown",
                tracking_number=tracking,
                carrier=carrier,
                items=[],
                order_date=lines[0] if lines else "",
            ))
        except Exception as exc:
            log.debug("Skipping order row: %s", exc)
            continue

    log.info("Found %d orders", len(orders))
    return orders


async def get_order_detail(page: Page, order_id: str) -> Optional[BiogentsOrder]:
    """Fetch full details for a single order including tracking and items."""
    detail_urls = [
        f"{BIOGENTS_URL}/account/orders/{order_id}",
        f"{BIOGENTS_URL}/my-account/view-order/{order_id}",
    ]
    for url in detail_urls:
        try:
            resp = await page.goto(url, wait_until="networkidle", timeout=10000)
            if resp and resp.ok:
                break
        except Exception:
            continue

    content = await page.inner_text("body")
    lines = [l.strip() for l in content.splitlines() if l.strip()]

    tracking = ""
    carrier = ""
    status = ""
    ship_name = ""
    ship_addr = ""
    items = []

    for line in lines:
        ll = line.lower()
        if "tracking" in ll:
            # next token is likely the number
            parts = line.split()
            for p in parts:
                if len(p) > 8 and p.replace("-", "").isalnum():
                    tracking = p
        if any(s in ll for s in ["shipped", "delivered", "processing", "completed", "pending"]):
            status = line
        if "ups" in ll:
            carrier = "UPS"
        elif "usps" in ll or "9400" in line:
            carrier = "USPS"
        elif "fedex" in ll:
            carrier = "FedEx"

    return BiogentsOrder(
        order_id=order_id,
        status=status or "unknown",
        tracking_number=tracking,
        carrier=carrier,
        items=items,
        order_date="",
        ship_to_name=ship_name,
        ship_to_address=ship_addr,
    )


# ---------------------------------------------------------------------------
# Inventory / product catalog
# ---------------------------------------------------------------------------

async def get_products(page: Page) -> list[BiogentsProduct]:
    """Return available products from the store/catalog page."""
    products = []

    catalog_urls = [
        f"{BIOGENTS_URL}/shop",
        f"{BIOGENTS_URL}/products",
        f"{BIOGENTS_URL}/store",
    ]
    for url in catalog_urls:
        try:
            resp = await page.goto(url, wait_until="networkidle", timeout=10000)
            if resp and resp.ok:
                break
        except Exception:
            continue

    # WooCommerce / Shopify product cards
    product_els = await page.query_selector_all(
        ".product, .woocommerce-LoopProduct, [class*='product-item'], [class*='product-card']"
    )

    for el in product_els:
        try:
            text = await el.inner_text()
            lines = [l.strip() for l in text.splitlines() if l.strip()]
            if not lines:
                continue

            name = lines[0]
            price = 0.0
            sku = ""
            in_stock = True

            for line in lines:
                if "$" in line:
                    try:
                        price = float(line.replace("$", "").replace(",", "").strip().split()[0])
                    except ValueError:
                        pass
                if "sku" in line.lower() or "item" in line.lower():
                    parts = line.split(":")
                    if len(parts) > 1:
                        sku = parts[1].strip()
                if "out of stock" in line.lower():
                    in_stock = False

            products.append(BiogentsProduct(
                sku=sku or name[:20].replace(" ", "-").lower(),
                name=name,
                price=price,
                in_stock=in_stock,
            ))
        except Exception as exc:
            log.debug("Skipping product: %s", exc)

    log.info("Found %d products", len(products))
    return products


# ---------------------------------------------------------------------------
# Place order (drop-ship or warehouse restock)
# ---------------------------------------------------------------------------

async def place_order(
    page: Page,
    sku: str,
    quantity: int,
    ship_to_name: str = "",
    ship_to_address: str = "",
    ship_to_city: str = "",
    ship_to_state: str = "",
    ship_to_zip: str = "",
    ship_to_phone: str = "",
) -> str:
    """
    Add a product to cart and complete checkout.
    If ship_to_* fields are provided, uses them for drop-ship to customer.
    Otherwise ships to the Greenguard warehouse address from .env.

    Returns the Biogents order ID string.
    """
    drop_ship = bool(ship_to_name and ship_to_address)
    destination = f"{ship_to_name} at {ship_to_address}" if drop_ship else "Greenguard warehouse"
    log.info("Placing order: sku=%s qty=%d → %s", sku, quantity, destination)

    # Navigate to product — try direct search or catalog
    search_urls = [
        f"{BIOGENTS_URL}/shop/?s={sku}",
        f"{BIOGENTS_URL}/products/?search={sku}",
        f"{BIOGENTS_URL}/shop",
    ]
    for url in search_urls:
        try:
            resp = await page.goto(url, wait_until="networkidle", timeout=10000)
            if resp and resp.ok:
                break
        except Exception:
            continue

    # Try to find and click the product matching the SKU
    found = False
    product_links = await page.query_selector_all("a.woocommerce-LoopProduct-link, .product a, [class*='product'] a")
    for link in product_links:
        text = (await link.inner_text()).strip().lower()
        href = await link.get_attribute("href") or ""
        if sku.lower() in text or sku.lower() in href.lower():
            await link.click()
            await page.wait_for_load_state("networkidle")
            found = True
            break

    if not found:
        log.info("Product not found by selector — using Gemini vision")
        await _gemini_navigate(page, f"Find and click the product with SKU or name: {sku}")

    # Set quantity if field exists
    try:
        qty_input = await page.query_selector('input[name="quantity"], input.qty, input[type="number"]')
        if qty_input:
            await qty_input.fill(str(quantity))
    except Exception:
        pass

    # Add to cart
    add_selectors = [
        'button.single_add_to_cart_button',
        'button[name="add-to-cart"]',
        'button:has-text("Add to Cart")',
        'button:has-text("Add to cart")',
        'input[value="Add to cart"]',
    ]
    added = False
    for sel in add_selectors:
        try:
            await page.click(sel, timeout=3000)
            added = True
            break
        except Exception:
            continue

    if not added:
        await _gemini_navigate(page, f"Add {quantity} of the current product to cart")

    await page.wait_for_load_state("networkidle")

    # Proceed to checkout
    checkout_selectors = [
        'a[href*="checkout"]', 'a:has-text("Checkout")',
        'a:has-text("Proceed to Checkout")', '.checkout-button',
    ]
    for sel in checkout_selectors:
        try:
            await page.click(sel, timeout=3000)
            break
        except Exception:
            continue

    await page.wait_for_load_state("networkidle")

    # Fill shipping address if drop-shipping to customer
    if drop_ship:
        # Use Greenguard email for account — billing stays on Greenguard
        ship_fields = {
            'input[name*="first_name"], #shipping_first_name': ship_to_name.split()[0],
            'input[name*="last_name"], #shipping_last_name': ship_to_name.split()[-1] if len(ship_to_name.split()) > 1 else "",
            'input[name*="address_1"], #shipping_address_1': ship_to_address,
            'input[name*="city"], #shipping_city': ship_to_city,
            'input[name*="postcode"], #shipping_postcode': ship_to_zip,
            'input[name*="phone"], #billing_phone': ship_to_phone or "",
        }
        for sel, val in ship_fields.items():
            if not val:
                continue
            for s in sel.split(", "):
                try:
                    await page.fill(s.strip(), val, timeout=2000)
                    break
                except Exception:
                    continue

        # State select
        if ship_to_state:
            for state_sel in ['select[name*="state"]', '#shipping_state']:
                try:
                    await page.select_option(state_sel, value=ship_to_state, timeout=2000)
                    break
                except Exception:
                    continue

    # Place order
    place_selectors = [
        '#place_order', 'button[name="woocommerce_checkout_place_order"]',
        'button:has-text("Place Order")', 'button:has-text("Submit Order")',
        'button[type="submit"]:has-text("order")',
    ]
    placed = False
    for sel in place_selectors:
        try:
            await page.click(sel, timeout=3000)
            placed = True
            break
        except Exception:
            continue

    if not placed:
        await _gemini_navigate(page, "Place the order by clicking the Place Order or Submit button")

    await page.wait_for_load_state("networkidle")

    # Extract order ID from confirmation page
    content = await page.inner_text("body")
    import re
    m = re.search(r"(?:order|#)\s*(?:number|#)?\s*:?\s*(\d{4,10})", content, re.IGNORECASE)
    order_id = m.group(1) if m else ""

    log.info("Order placed — Biogents order ID: %s (URL: %s)", order_id or "unknown", page.url)
    return order_id


# ---------------------------------------------------------------------------
# Convenience: run with a managed browser context
# ---------------------------------------------------------------------------

async def run_with_browser(coro_fn, headless: bool = True):
    """
    Run an async function that receives a logged-in Playwright page.
    Usage:
        result = await run_with_browser(lambda page: get_orders(page))
    """
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)
        context = await browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        )
        page = await context.new_page()
        try:
            await login(page)
            return await coro_fn(page)
        finally:
            await browser.close()


# ---------------------------------------------------------------------------
# Standalone test
# ---------------------------------------------------------------------------

async def _test():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if not BIOGENTS_EMAIL or not BIOGENTS_PASSWORD:
        print("ERROR: BIOGENTS_EMAIL and BIOGENTS_PASSWORD must be set in .env")
        return

    print(f"Logging in as {BIOGENTS_EMAIL} ...")

    async def _run(page):
        orders = await get_orders(page)
        print(f"\n=== Orders ({len(orders)}) ===")
        for o in orders[:5]:
            print(f"  #{o.order_id}  status={o.status}  tracking={o.tracking_number or 'none'}")

        products = await get_products(page)
        print(f"\n=== Products ({len(products)}) ===")
        for prod in products[:10]:
            print(f"  {prod.sku}  {prod.name}  ${prod.price}  in_stock={prod.in_stock}")

    await run_with_browser(_run, headless=False)


if __name__ == "__main__":
    asyncio.run(_test())
