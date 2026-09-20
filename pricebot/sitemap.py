"""Sitemap fallback.

Many shops have a search page that is JavaScript-only, bot-walled, or simply uses a URL we
cannot guess. Almost all of them, however, publish a plain XML sitemap for Google - static
files, no JS, rarely protected. Product slugs usually contain the part number
(`…/shimano-ultegra-di2-rd-r8150-12-speed-rear-derailleur`), so the sitemap alone is enough to
locate a product page.

The URL list is downloaded once and cached in data/sitemaps/<shop>.txt for `sitemap_max_age_days`.
"""
from __future__ import annotations

import gzip
import html
import io
import re
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

import requests

from .config import DATA, Shop, Sku

CACHE = DATA / "sitemaps"
_LOC_RE = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>", re.I)
_SITEMAP_RE = re.compile(r"<sitemapindex", re.I)
_SKIP_RE = re.compile(
    r"/(blog|clanek|clanky|article|news|magazin|magazine|ratgeber|advice|kategorie|category|"
    r"kategorien|collections?|brand|marke|znacka|login|cart|kosik|account|info|page|stranka)/",
    re.I,
)


def _deslug(url: str) -> str:
    """URL -> text that title rules can be applied to: '…/rd-r8150-12-speed' -> 'rd-r8150 12 speed'."""
    path = re.sub(r"^https?://[^/]+", "", url)
    path = re.sub(r"\.(html?|php|aspx)$", "", path, flags=re.I)
    return re.sub(r"[/_+%\-]+", " ", path).strip()


def _fetch_text(session: requests.Session, url: str, timeout: int) -> str:
    r = session.get(url, timeout=timeout)
    r.raise_for_status()
    body = r.content
    if url.endswith(".gz") or body[:2] == b"\x1f\x8b":
        with gzip.GzipFile(fileobj=io.BytesIO(body)) as fh:
            body = fh.read()
    return body.decode("utf-8", errors="replace")


def _sitemap_urls_from_robots(session, base: str, timeout: int) -> list[str]:
    try:
        txt = _fetch_text(session, f"{base}/robots.txt", timeout)
    except Exception:
        return []
    return re.findall(r"(?im)^\s*sitemap:\s*(\S+)", txt)


def collect_urls(shop: Shop, session: requests.Session, settings: dict,
                 log: Callable[[str], None] = print) -> list[str]:
    """All product-ish URLs from the shop's sitemap(s), cached on disk."""
    CACHE.mkdir(parents=True, exist_ok=True)
    cache = CACHE / f"{shop.id}.txt"
    max_age = int(settings.get("sitemap_max_age_days", 7))
    if cache.exists():
        age = date.today() - datetime.fromtimestamp(cache.stat().st_mtime).date()
        if age <= timedelta(days=max_age):
            return cache.read_text(encoding="utf-8").splitlines()

    base = re.match(r"https?://[^/]+", shop.search_urls[0]).group(0)
    timeout = int(settings["timeout"])
    roots = _sitemap_urls_from_robots(session, base, timeout)
    if not roots:
        roots = [f"{base}{path}" for path in (
            "/sitemap.xml", "/sitemap_index.xml", "/sitemap-index.xml", "/sitemap/sitemap.xml",
            "/sitemap/index.xml", "/sitemaps/sitemap.xml", "/sitemap_index.xml.gz", "/sitemap.xml.gz",
        )]
    max_files = int(settings.get("sitemap_max_files", 40))
    delay = float(shop.delay if shop.delay is not None else settings.get("delay", 1.5))
    seen_files: set[str] = set()
    queue = list(roots)
    urls: list[str] = []
    refused = False

    while queue and len(seen_files) < max_files:
        src = queue.pop(0)
        if src in seen_files:
            continue
        if seen_files:
            time.sleep(delay)              # same politeness as page fetches - a sitemap can be 100+ files
        seen_files.add(src)
        try:
            xml = _fetch_text(session, src, timeout)
        except requests.HTTPError as exc:
            log(f"[{shop.id}] sitemap {src}: {exc!r}")
            if exc.response is not None and exc.response.status_code in (403, 429, 503):
                refused = True             # the shop told us to stop - stop, and don't keep the partial list
                break
            continue
        except Exception as exc:
            log(f"[{shop.id}] sitemap {src}: {exc!r}")
            continue
        locs = [html.unescape(u) for u in _LOC_RE.findall(xml)]
        if locs and src in roots:
            roots = [src]          # tenhle root funguje, další fallback cesty nezkoušej
        if _SITEMAP_RE.search(xml):
            # An index: prefer child sitemaps that look product-related.
            # (matched on the path only, and "item" must not be the one inside "sitemap")
            prod = [u for u in locs if re.search(r"produkt|product|(?<!s)item|artikel|shop", urlparse(u).path, re.I)]
            queue += (prod or locs)
        else:
            urls += locs

    urls = [u for u in dict.fromkeys(urls) if not _SKIP_RE.search(u)]
    if refused:
        log(f"[{shop.id}] sitemap: shop odmítá další stahování (429/403/503) – končím, neúplný seznam neukládám")
        return urls
    if not urls:                       # failed harvest must not be cached for a week
        log(f"[{shop.id}] sitemap: nic nestaženo (blokace nebo chybí sitemap.xml)")
        return []
    cache.write_text("\n".join(urls), encoding="utf-8")
    log(f"[{shop.id}] sitemap: {len(urls)} URL z {len(seen_files)} souborů (cache {max_age} dní)")
    return urls


def candidates(sku: Sku, urls: list[str], limit: int = 5) -> list[tuple[str, str]]:
    """URLs whose slug satisfies the SKU's title rules -> [(url, deslugged text)]."""
    out: list[tuple[str, str]] = []
    for u in urls:
        text = _deslug(u)
        if not text:
            continue
        if all(p.search(text) for p in sku.must_match) and not any(p.search(text) for p in sku.must_not_match):
            out.append((u, text))
            if len(out) >= limit:
                break
    return out
