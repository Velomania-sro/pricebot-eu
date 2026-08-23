"""Optional fallback: ask Claude to extract name/price/currency/availability from visible page text.

Only used when ANTHROPIC_API_KEY is set and the page has no JSON-LD / meta price.
Cost is tiny (Haiku, ~2k input tokens per page) and it only triggers on parser misses.
"""
from __future__ import annotations

import json
import os
import re

from .config import Sku
from .parse import Offer, norm_availability, visible_text

_PROMPT = """Níže je text produktové stránky cyklistického e-shopu. Vrať POUZE JSON (bez markdownu) ve tvaru:
{"name": "<přesný název produktu>", "price": <číslo nebo null>, "currency": "<EUR|CZK|PLN|GBP|...>", "availability": "<in_stock|out|preorder|unknown>"}

Pravidla:
- price = aktuální prodejní cena (po slevě) hlavního produktu na stránce, včetně DPH, jako číslo bez mezer a měnových symbolů.
- Pokud stránka není produktová (výpis, 404, košík) nebo cena není jednoznačná, vrať {"price": null}.
- Nevymýšlej si. Neposuzuj, zda jde o hledaný produkt – to řeším jinde.

URL: {url}

TEXT:
{text}"""


def enabled() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def claude_extract(html: str, url: str, sku: Sku, model: str) -> Offer | None:
    if not enabled():
        return None
    try:
        import anthropic  # lazy import – optional dependency
    except ImportError:
        return None
    text = visible_text(html, 7000)
    if len(text) < 200:
        return None
    try:
        client = anthropic.Anthropic()
        msg = client.messages.create(
            model=model,
            max_tokens=300,
            messages=[{"role": "user", "content": _PROMPT.replace("{url}", url).replace("{text}", text)}],
        )
        raw = "".join(getattr(b, "text", "") for b in msg.content)
        raw = re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.M).strip()
        data = json.loads(raw)
    except Exception:
        return None
    price = data.get("price")
    if not isinstance(price, (int, float)) or price <= 0:
        return None
    return Offer(
        name=str(data.get("name") or ""),
        price=float(price),
        currency=str(data.get("currency") or "").upper(),
        availability=norm_availability(data.get("availability")),
        url=url,
        source="claude",
    )
