"""Polite page fetcher: one instance per shop (own session, own delay, optional Playwright)."""
from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass

import requests

from .config import Shop

_BLOCK_RE = re.compile(
    r"cf-chl-bypass|Just a moment\.\.\.|Attention Required! \| Cloudflare|Access Denied|"
    r"Pardon Our Interruption|_Incapsula_Resource|Request unsuccessful\. Incapsula|"
    r"Bot detection|Please verify you are a human|DataDome",
    re.I,
)


class Blocked(Exception):
    """Shop refused to serve us (403/429/503 or a bot-wall page)."""


@dataclass
class Page:
    status: int
    text: str
    url: str


def looks_blocked(status: int, text: str) -> bool:
    if status in (403, 429, 503):
        return True
    # Bot walls are short pages; a normal product/search page is much longer.
    if status != 200 or len(text) < 6000:
        return bool(_BLOCK_RE.search(text or ""))
    return False


class Fetcher:
    def __init__(self, shop: Shop, settings: dict):
        self.shop = shop
        self.settings = settings
        self.delay = shop.delay if shop.delay is not None else settings["delay"]
        self.timeout = settings["timeout"]
        self._last = 0.0
        self._pw = self._browser = self._ctx = None
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": settings["user_agent"],
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": shop.accept_language or "en-GB,en;q=0.9,de;q=0.8,cs;q=0.7",
                "Cache-Control": "no-cache",
            }
        )
        if shop.headers:
            self.session.headers.update(shop.headers)
        if shop.cookies:
            for k, v in shop.cookies.items():
                self.session.cookies.set(k, str(v))
        proxy = os.environ.get("PRICEBOT_PROXY")
        if proxy:
            self.session.proxies = {"http": proxy, "https": proxy}

    # -- public ---------------------------------------------------------
    def get(self, url: str) -> Page:
        self._wait()
        page = self._get_playwright(url) if self.shop.fetcher == "playwright" else self._get_requests(url)
        if looks_blocked(page.status, page.text):
            raise Blocked(f"{self.shop.id}: HTTP {page.status} for {url}")
        return page

    def close(self) -> None:
        try:
            if self._browser:
                self._browser.close()
            if self._pw:
                self._pw.stop()
        except Exception:
            pass
        self.session.close()

    # -- internals ------------------------------------------------------
    def _wait(self) -> None:
        dt = time.monotonic() - self._last
        if dt < self.delay:
            time.sleep(self.delay - dt)
        self._last = time.monotonic()

    def _get_requests(self, url: str) -> Page:
        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                r = self.session.get(url, timeout=self.timeout, allow_redirects=True)
                return Page(r.status_code, r.text, r.url)
            except (requests.ConnectionError, requests.Timeout) as exc:
                last_exc = exc
                time.sleep(2 * (attempt + 1))
        raise requests.RequestException(f"{self.shop.id}: {last_exc!r}")

    def _get_playwright(self, url: str) -> Page:
        if self._pw is None:
            from playwright.sync_api import sync_playwright  # lazy import

            self._pw = sync_playwright().start()
            launch: dict = {"headless": True}
            proxy = os.environ.get("PRICEBOT_PROXY")
            if proxy:
                launch["proxy"] = {"server": proxy}
            self._browser = self._pw.chromium.launch(**launch)
            self._ctx = self._browser.new_context(
                user_agent=self.settings["user_agent"],
                locale="en-GB",
                viewport={"width": 1366, "height": 900},
            )
        page = self._ctx.new_page()
        try:
            resp = page.goto(url, wait_until="domcontentloaded", timeout=int(self.timeout * 1000))
            page.wait_for_timeout(1500)  # let late JSON-LD / price widgets render
            return Page(resp.status if resp else 0, page.content(), page.url)
        finally:
            page.close()
