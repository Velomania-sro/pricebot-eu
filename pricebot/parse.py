"""HTML parsing: product offers (JSON-LD first, meta tags second) and search-result candidates."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

_AVAIL_RANK = {"in_stock": 0, "limited": 1, "preorder": 2, "backorder": 2, "unknown": 3, "out": 4}


@dataclass
class Offer:
    name: str
    price: float
    currency: str
    availability: str   # in_stock | limited | preorder | backorder | out | unknown
    url: str
    source: str         # jsonld | meta | claude
    region: str = ""    # eligibleRegion of a geo-priced offer (country code, e.g. "DE"), else ""


# ---------------------------------------------------------------- prices
def parse_price(raw) -> float | None:
    """'1.299,00' / '1,299.00' / '12 990 Kč' / 1299 -> float."""
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    s = re.sub(r"[^\d,.\-]", "", str(raw))
    if not s or not re.search(r"\d", s):
        return None
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):      # 1.299,00
            s = s.replace(".", "").replace(",", ".")
        else:                                 # 1,299.00
            s = s.replace(",", "")
    elif "," in s:
        if re.fullmatch(r"-?\d{1,3}(,\d{3})+", s):   # 1,299  (thousands)
            s = s.replace(",", "")
        else:                                        # 1299,00
            s = s.replace(",", ".")
    elif "." in s:
        if re.fullmatch(r"-?\d{1,3}(\.\d{3})+", s):  # 1.299  (thousands)
            s = s.replace(".", "")
    try:
        v = float(s)
    except ValueError:
        return None
    return v if v > 0 else None


def norm_availability(v) -> str:
    s = str(v or "").lower().replace("_", "").replace("-", "").replace(" ", "")
    for key, out in (
        ("instock", "in_stock"), ("instoreonly", "in_stock"), ("onlineonly", "in_stock"),
        ("limitedavailability", "limited"), ("preorder", "preorder"), ("presale", "preorder"),
        ("backorder", "backorder"), ("outofstock", "out"), ("soldout", "out"), ("discontinued", "out"),
    ):
        if key in s:
            return out
    return "unknown"


# ---------------------------------------------------------------- JSON-LD
def _as_list(x) -> list:
    if x is None:
        return []
    return x if isinstance(x, list) else [x]


def _types(obj: dict) -> list[str]:
    t = obj.get("@type")
    if isinstance(t, str):
        return [t]
    return [x for x in _as_list(t) if isinstance(x, str)]


def _flatten(data) -> list[dict]:
    if isinstance(data, list):
        return [o for d in data for o in _flatten(d)]
    if isinstance(data, dict):
        out = [data]
        for key in ("@graph", "mainEntity"):
            if key in data:
                out += _flatten(data[key])
        return out
    return []


def extract_jsonld(html: str) -> list[dict]:
    soup = BeautifulSoup(html or "", "lxml")
    out: list[dict] = []
    for tag in soup.find_all("script", attrs={"type": re.compile(r"ld\+json", re.I)}):
        txt = (tag.string or tag.get_text() or "").strip()
        if not txt:
            continue
        data = None
        for candidate in (txt, re.sub(r"[\x00-\x1f]", " ", txt)):
            try:
                data = json.loads(candidate)
                break
            except json.JSONDecodeError:
                continue
        if data is not None:
            out.extend(_flatten(data))
    return out


def _region_of(o: dict) -> str:
    """eligibleRegion / areaServed of an offer -> a country code like 'DE' (best effort)."""
    for x in _as_list(o.get("eligibleRegion") or o.get("areaServed")):
        if isinstance(x, str) and x.strip():
            return x.strip()
        if isinstance(x, dict):
            v = x.get("addressCountry") or x.get("name") or x.get("identifier")
            if isinstance(v, dict):
                v = v.get("name") or v.get("@value")
            if v:
                return str(v).strip()
    return ""


def _one_offer(o: dict, key: str = "price") -> list[dict]:
    price = parse_price(o.get(key))
    if price is None and key != "price":
        price = parse_price(o.get("price"))
    cur = o.get("priceCurrency")
    if price is None:
        for ps in _as_list(o.get("priceSpecification")):
            if isinstance(ps, dict):
                price = parse_price(ps.get("price"))
                cur = cur or ps.get("priceCurrency")
                if price is not None:
                    break
    if price is None:
        return []
    return [{"price": price, "currency": str(cur or "").upper(),
             "availability": norm_availability(o.get("availability")), "region": _region_of(o)}]


def _offers_of(prod: dict) -> list[dict]:
    out: list[dict] = []
    offers = prod.get("offers")
    if offers is None and prod.get("hasVariant"):          # schema.org ProductGroup
        for v in _as_list(prod.get("hasVariant")):
            if isinstance(v, dict):
                out += _offers_of(v)
        return out
    for o in _as_list(offers):
        if not isinstance(o, dict):
            continue
        nested = [s for s in _as_list(o.get("offers")) if isinstance(s, dict)]
        for s in nested:
            out += _one_offer(s)
        if "aggregateoffer" in [t.lower() for t in _types(o)]:
            out += _one_offer(o, key="lowPrice")
        elif not nested:
            out += _one_offer(o)
    return out


def _name_of(prod: dict) -> str:
    n = prod.get("name") or ""
    if isinstance(n, dict):
        n = n.get("@value") or ""
    return " ".join(str(n).split())


def _meta(soup: BeautifulSoup, *names: str) -> str | None:
    for n in names:
        for attr in ("property", "name", "itemprop"):
            tag = soup.find("meta", attrs={attr: n})
            if tag and tag.get("content"):
                return tag["content"]
    return None


def parse_product_page(html: str, url: str) -> list[Offer]:
    """All offers found on a product page (caller filters by title and picks the best)."""
    offers: list[Offer] = []
    for obj in extract_jsonld(html):
        if not any(t.lower() in ("product", "productgroup", "individualproduct") for t in _types(obj)):
            continue
        name = _name_of(obj)
        for off in _offers_of(obj):
            offers.append(Offer(name, off["price"], off["currency"], off["availability"], url, "jsonld",
                                off.get("region", "")))
    if offers:
        return offers

    # Fallback: Open Graph / microdata
    soup = BeautifulSoup(html or "", "lxml")
    price = _meta(soup, "product:price:amount", "og:price:amount", "price")
    cur = _meta(soup, "product:price:currency", "og:price:currency", "priceCurrency")
    avail = _meta(soup, "product:availability", "og:availability")
    if price is None:
        tag = soup.find(attrs={"itemprop": "price"})
        if tag:
            price = tag.get("content") or tag.get_text(" ", strip=True)
        ctag = soup.find(attrs={"itemprop": "priceCurrency"})
        if ctag and not cur:
            cur = ctag.get("content")
        atag = soup.find(attrs={"itemprop": "availability"})
        if atag and not avail:
            avail = atag.get("href") or atag.get("content")
    p = parse_price(price)
    if p is None:
        return []
    name = _meta(soup, "og:title") or (soup.title.get_text(" ", strip=True) if soup.title else "")
    return [Offer(" ".join(name.split()), p, str(cur or "").upper(), norm_availability(avail), url, "meta")]


def best_offer(offers: list[Offer]) -> Offer | None:
    if not offers:
        return None
    return min(offers, key=lambda o: (_AVAIL_RANK.get(o.availability, 3), o.price))


# ---------------------------------------------------------------- search pages
def search_candidates(html: str, base_url: str, url_re: re.Pattern | None, key_re: re.Pattern | None, limit: int = 40) -> list[tuple[str, str]]:
    """Product links on a search-result page -> [(absolute_url, title)].

    1) JSON-LD ItemList (many shops emit it on category/search pages)
    2) anchors matching the shop's product_url_pattern
    3) heuristic: anchors whose text/href match the SKU key regex
    """
    cands: list[tuple[str, str]] = []
    seen: set[str] = set()
    base_host = urlparse(base_url).netloc
    base_path = urlparse(base_url).path.rstrip("/")

    def add(url: str, title: str) -> None:
        url = urljoin(base_url, url).split("#")[0]
        if url in seen or urlparse(url).netloc != base_host:
            return
        # Facet/filter links point back at the search page with extra query params - not products.
        if urlparse(url).path.rstrip("/") == base_path and urlparse(url).query:
            return
        seen.add(url)
        cands.append((url, " ".join((title or "").split())))

    for obj in extract_jsonld(html):
        if "itemlist" not in [t.lower() for t in _types(obj)]:
            continue
        for el in _as_list(obj.get("itemListElement")):
            if not isinstance(el, dict):
                continue
            item = el.get("item") if isinstance(el.get("item"), dict) else el
            u = item.get("url") or el.get("url") or item.get("@id")
            if u:
                add(u, _name_of(item) or _name_of(el))

    if cands:
        return cands[:limit]

    soup = BeautifulSoup(html or "", "lxml")
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if href.startswith(("javascript:", "mailto:", "tel:")):
            continue
        title = a.get_text(" ", strip=True) or a.get("title") or a.get("aria-label") or ""
        if not title:
            img = a.find("img")
            if img is not None:
                title = img.get("alt") or img.get("title") or ""
        if url_re is not None:
            if url_re.search(urljoin(base_url, href)):
                add(href, title)
        elif key_re is not None and key_re.search(f"{title} {href}"):
            add(href, title)
    return cands[:limit]


def visible_text(html: str, limit: int = 6000) -> str:
    soup = BeautifulSoup(html or "", "lxml")
    for t in soup(["script", "style", "noscript", "svg", "nav", "footer", "header"]):
        t.decompose()
    txt = " ".join(soup.get_text(" ", strip=True).split())
    return txt[:limit]
