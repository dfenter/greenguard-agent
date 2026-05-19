"""
Item 7 — Token usage logging.
Appends one row per Claude call to logs/usage_YYYY-MM-DD.csv.
Call log_usage() after every response; call daily_total() for the morning summary.
"""
import csv
from datetime import datetime
from pathlib import Path

_LOG_DIR = Path(__file__).parent / "logs"
_LOG_DIR.mkdir(exist_ok=True)

# $ per 1M tokens
_RATES: dict[str, dict[str, float]] = {
    "claude-haiku-4-5":   {"in": 1.00, "out": 5.00,  "cw": 1.25, "cr": 0.10},
    "claude-sonnet-4-6":  {"in": 3.00, "out": 15.00, "cw": 3.75, "cr": 0.30},
    "claude-opus-4-7":    {"in": 5.00, "out": 25.00, "cw": 6.25, "cr": 0.50},
}


def log_usage(model: str, usage, label: str = "") -> float:
    """Append usage row; return estimated cost in USD."""
    r = _RATES.get(model, _RATES["claude-haiku-4-5"])
    inp  = getattr(usage, "input_tokens", 0) or 0
    out  = getattr(usage, "output_tokens", 0) or 0
    cr   = getattr(usage, "cache_read_input_tokens", 0) or 0
    cw   = getattr(usage, "cache_creation_input_tokens", 0) or 0

    cost = (inp * r["in"] + out * r["out"] + cr * r["cr"] + cw * r["cw"]) / 1_000_000

    log_file = _LOG_DIR / f"usage_{datetime.now():%Y-%m-%d}.csv"
    new_file = not log_file.exists()
    with open(log_file, "a", newline="") as f:
        w = csv.writer(f)
        if new_file:
            w.writerow(["ts", "label", "model", "in", "out", "cache_read", "cache_write", "usd"])
        w.writerow([datetime.now().isoformat(timespec="seconds"),
                    label[:60], model, inp, out, cr, cw, f"{cost:.6f}"])
    return cost


def daily_total() -> tuple[int, float]:
    """Return (total_tokens, total_cost_usd) for today."""
    log_file = _LOG_DIR / f"usage_{datetime.now():%Y-%m-%d}.csv"
    if not log_file.exists():
        return 0, 0.0
    tokens, cost = 0, 0.0
    with open(log_file) as f:
        for row in csv.DictReader(f):
            tokens += int(row["in"]) + int(row["out"])
            cost += float(row["usd"])
    return tokens, cost
