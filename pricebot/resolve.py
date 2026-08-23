"""Title matching + resolving a SKU to a product URL on a given shop via its site search."""
from __future__ import annotations

from typing import Callable
from urllib.parse import quote_plus

import requests

from . import sitemap
from .config import Shop, Sku
from .fetch import Fetcher
from .parse import Offer, best_offer, parse_product_page, search_candidates


# ---------------------------------------------------------------- matching
def title_ok(title: str, sku: Sku) -> bool:
    t = title or ""
    return all(p.search(t) for p in sku.must_match) and not any(p.search(t) for p in sku.must_not_match)


def prefer_score(title: str, sku: Sku) -> int:
    t = title or ""
    return sum(1 for p in sku.prefer if p.search(t))


def prefilter_score(title: str, sku: Sku) -> int:
    """Cheap ranking of search-result anchors (titles may be truncated -> soft scoring)."""
    if not title:
        return 0
    s = 2 if all(p.search(title) for p in sku.must_match) else -1
    if any(p.search(title) for p in sku.must_not_match):
        s -= 5
    return s + prefer_score(title, sku)


def pick(offers: list[Offer], sku: Sku, currency: str | None = None) -> Offer | None:
    """Best offer among those whose title passes the SKU rules.

    Some shops (e.g. starbike) emit one geo-priced offer per country in that country's
    currency (EUR, GBP, OMR, …) in a single AggregateOffer. Comparing those raw numbers
    would pick the smallest *number* regardless of currency (435 OMR < 970 EUR). When the
    shop's own currency is known, restrict to offers in it before choosing the cheapest.
    """
    matches = [o for o in offers if title_ok(o.name, sku)]
    if currency:
        same = [o for o in matches if (o.currency or "").upper() == currency.upper()]
        if same:
            matches = same
    return best_offer(matches)


# ---------------------------------------------------------------- resolving
def resolve(
    sku: Sku,
    shop: Shop,
    fetcher: Fetcher,
    settings: dict,
    log: Callable[[str], None] = print,
    extra_parser: Callable[[str, str, Sku], Offer | None] | None = None,
) -> tuple[Offer, str] | None:
    """Search the shop for the SKU, open the best candidates, return (offer, url) or None."""
    max_c = int(settings["max_candidates"])
    key_re = sku.must_match[0] if sku.must_match else None
    leftovers: list[tuple[str, str]] = []

    queries = [(q, tpl) for q in sku.search for tpl in shop.search_urls]
    for q, tpl in queries:
        url = tpl.replace("{q}", quote_plus(q))
        try:
            page = fetcher.get(url)
        except requests.RequestException as exc:
            log(f"[{shop.id}] search failed for {sku.sku_id!r}: {exc}")
            continue

        # Some shops redirect a single hit straight to the product page.
        direct = pick(parse_product_page(page.text, page.url), sku, shop.currency)
        if direct:
            return direct, page.url

        cands = search_candidates(page.text, page.url, shop.url_re, key_re)
        scored = sorted(((prefilter_score(t, sku), u, t) for u, t in cands), key=lambda x: -x[0])
        strong = [(u, t) for s, u, t in scored if s >= 2]
        if strong:
            found = _open_candidates(strong[:max_c], sku, fetcher, log, extra_parser)
            if found:
                return found
        leftovers += [(u, t) for s, u, t in scored if -3 < s < 2]

    # Nothing with a convincing title: try the best of the rest (once).
    if leftovers:
        found = _open_candidates(leftovers[:max_c], sku, fetcher, log, extra_parser)
        if found:
            return found

    # Site search unusable (JS-only, bot-walled, unknown URL)? Try the shop's sitemap.
    if settings.get("use_sitemap", True):
        try:
            urls = sitemap.collect_urls(shop, fetcher.session, settings, log)
        except Exception as exc:
            log(f"[{shop.id}] sitemap failed: {exc!r}")
            return None
        cands = sitemap.candidates(sku, urls)
        if cands:
            return _open_candidates(cands[:max_c], sku, fetcher, log, extra_parser)
    return None


def _open_candidates(cands, sku, fetcher, log, extra_parser) -> tuple[Offer, str] | None:
    best: tuple[tuple[int, float], Offer, str] | None = None
    for curl, _title in cands:
        try:
            page = fetcher.get(curl)
        except requests.RequestException as exc:
            log(f"[{fetcher.shop.id}] product fetch failed: {exc}")
            continue
        if page.status >= 400:
            continue
        offer = pick(parse_product_page(page.text, page.url), sku, fetcher.shop.currency)
        if offer is None and extra_parser is not None:
            o = extra_parser(page.text, page.url, sku)
            if o is not None and title_ok(o.name, sku):
                offer = o
        if offer is None:
            continue
        key = (prefer_score(offer.name, sku), -offer.price)
        if best is None or key > best[0]:
            best = (key, offer, page.url)
    return (best[1], best[2]) if best else None
