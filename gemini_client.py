"""
Gemini AI client for Greenguard USA.

Uses Google Cloud Application Default Credentials (ADC) — no API key needed.
Credentials live at ~/.config/gcloud/application_default_credentials.json.

Two capabilities:
  1. draft_shipping_notification() — generate customer-facing shipping email body
  2. decide_next_action()          — vision-guided Playwright navigation
"""

import google.auth
import google.generativeai as genai

_model = None


def _get_model():
    global _model
    if _model is None:
        credentials, _ = google.auth.default(
            scopes=["https://www.googleapis.com/auth/generative-language"]
        )
        genai.configure(credentials=credentials)
        _model = genai.GenerativeModel("gemini-2.0-flash")
    return _model


def draft_shipping_notification(
    customer_name: str,
    product_name: str,
    tracking_number: str,
    carrier: str,
    order_id: str = "",
) -> str:
    """Return a plain-text email body notifying a customer their equipment has shipped."""
    first = customer_name.split()[0] if customer_name else "there"
    prompt = (
        f"Write a short, friendly plain-text email body notifying a customer that their "
        f"CO2 mosquito trap equipment has shipped. "
        f"Customer first name: {first}. "
        f"Product: {product_name}. "
        f"Carrier: {carrier}. "
        f"Tracking number: {tracking_number}. "
        + (f"Order ID: {order_id}. " if order_id else "")
        + "The company is Greenguard USA (mosquito control, Austin TX). "
        f"Close with 'Best regards,\\nGreenguard USA\\nadmin@greenguard-usa.com'. "
        f"Do not include a subject line. 3-4 sentences max. No em dashes."
    )
    model = _get_model()
    response = model.generate_content(prompt)
    return response.text.strip()


def decide_next_action(screenshot_bytes: bytes, goal: str) -> dict:
    """
    Given a Playwright page screenshot and a goal string, return the next browser action.

    Returns a dict with keys:
      action:   "click" | "fill" | "select" | "done" | "error"
      selector: CSS selector or text to target (for click/fill/select)
      value:    text to type (for fill/select)
      reason:   brief explanation
    """
    import base64

    img_b64 = base64.b64encode(screenshot_bytes).decode()
    prompt = (
        f"You are controlling a browser to accomplish this goal: {goal}\n\n"
        "Look at the screenshot and return the single next action as JSON with these fields:\n"
        '  "action": "click" | "fill" | "select" | "done" | "error"\n'
        '  "selector": CSS selector or visible text of the element to target\n'
        '  "value": text to type or select (empty string if not needed)\n'
        '  "reason": one sentence explaining the action\n\n'
        "Return only valid JSON, no markdown fences."
    )
    model = _get_model()
    image_part = {"mime_type": "image/png", "data": img_b64}
    response = model.generate_content([prompt, image_part])

    import json
    text = response.text.strip().strip("```json").strip("```").strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"action": "error", "selector": "", "value": "", "reason": text}
