"""EUR exchange rates (ECB reference rates via frankfurter) with on-disk cache and static fallback."""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import requests

from .config import DATA

FALLBACK = {"EUR": 1.0, "CZK": 24.6, "PLN": 4.27, "GBP": 0.85, "CHF": 0.94, "DKK": 7.46, "SEK": 11.2, "HUF": 395.0}
SOURCES = (
    "https://api.frankfurter.dev/v1/latest?base=EUR",
    "https://api.frankfurter.app/latest?from=EUR",
)


def get_rates(cache: Path = DATA / "fx.json") -> dict[str, float]:
    """Returns {currency: units per 1 EUR}."""
    for src in SOURCES:
        try:
            r = requests.get(src, timeout=15)
            r.raise_for_status()
            rates = {k: float(v) for k, v in r.json()["rates"].items()}
            rates["EUR"] = 1.0
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps({"date": date.today().isoformat(), "rates": rates}), encoding="utf-8")
            return rates
        except Exception:
            continue
    if cache.exists():
        try:
            return json.loads(cache.read_text(encoding="utf-8"))["rates"]
        except Exception:
            pass
    return dict(FALLBACK)


def to_eur(amount: float, currency: str, rates: dict[str, float]) -> float | None:
    rate = rates.get((currency or "EUR").upper())
    if not rate:
        return None
    return amount / rate
