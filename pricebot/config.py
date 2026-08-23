"""Configuration: shops (shops.yaml) and SKU master list (skus.csv)."""
from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"

DEFAULT_SETTINGS: dict = {
    "user_agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "timeout": 25,            # seconds per request
    "delay": 1.5,             # seconds between requests to the same shop
    "max_candidates": 3,      # product pages opened per SKU when resolving a URL
    "max_blocks_per_shop": 3, # after N blocked responses the shop is skipped for this run
    "parallel_shops": 4,      # shops scraped concurrently (one worker per shop)
    "alert_pct": 3.0,         # min % change of the cheapest price to appear in "Změny"
    "history_days_for_min": 30,
    "use_sitemap": True,        # fallback: najít produkt v sitemap.xml, když selže vyhledávání
    "sitemap_max_files": 150,
    "sitemap_max_age_days": 7,
    "claude_model": "claude-haiku-4-5-20251001",
    "sheet": {
        "matrix": "Matice",
        "minimum": "Minimum",
        "detail": "Detail",
        "changes": "Změny",
        "history": "Historie min",
    },
}


@dataclass
class Shop:
    id: str
    name: str
    country: str
    currency: str
    vat: float
    search_url: str | list[str]         # must contain {q}; a list = candidate templates to try
    product_url_pattern: str | None = None  # regex recognising product links on a search page
    fetcher: str = "requests"           # "requests" | "playwright"
    delay: float | None = None
    accept_language: str | None = None
    cookies: dict | None = None      # např. {currency: EUR} pro shopy s geo-cenami
    headers: dict | None = None
    enabled: bool = True
    verified: bool = False
    notes: str = ""

    @property
    def url_re(self) -> re.Pattern | None:
        return re.compile(self.product_url_pattern, re.I) if self.product_url_pattern else None

    @property
    def search_urls(self) -> list[str]:
        """Candidate search-URL templates, in order of preference."""
        urls = [self.search_url] if isinstance(self.search_url, str) else list(self.search_url)
        bad = [u for u in urls if "{q}" not in u]
        if bad:
            raise ValueError(f"shop {self.id}: search_url without {{q}}: {bad}")
        return urls


@dataclass
class Sku:
    sku_id: str
    brand: str
    series: str
    generation: str
    category: str
    name: str
    unit: str
    search: list[str]
    must_match: list[re.Pattern]
    must_not_match: list[re.Pattern]
    prefer: list[re.Pattern]
    part_number: str = ""
    notes: str = ""
    raw: dict = field(default_factory=dict)


def _split(s: str | None) -> list[str]:
    return [x.strip() for x in (s or "").split(";") if x.strip()]


def _compile(frags: list[str]) -> list[re.Pattern]:
    return [re.compile(f, re.I) for f in frags]


def load_settings(path: Path = ROOT / "shops.yaml") -> dict:
    cfg = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    settings = dict(DEFAULT_SETTINGS)
    settings["sheet"] = dict(DEFAULT_SETTINGS["sheet"])
    user = cfg.get("settings") or {}
    for k, v in user.items():
        if k == "sheet" and isinstance(v, dict):
            settings["sheet"].update(v)
        else:
            settings[k] = v
    return settings


def load_overrides(path: Path = ROOT / "shops.local.yaml") -> dict[str, dict]:
    """Per-shop overrides written by `probe --fix`; keeps shops.yaml (and its comments) untouched."""
    if not path.exists():
        return {}
    cfg = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return {o["id"]: {k: v for k, v in o.items() if k != "id"} for o in (cfg.get("shops") or [])}


def save_overrides(overrides: dict[str, dict], path: Path = ROOT / "shops.local.yaml") -> None:
    data = {"shops": [{"id": sid, **vals} for sid, vals in sorted(overrides.items())]}
    header = ("# Automaticky zapsáno příkazem `python -m pricebot probe --fix`.\n"
              "# Přepisuje hodnoty ze shops.yaml podle toho, co u shopu reálně funguje.\n"
              "# Klidně edituj ručně; smazání souboru vrátí vše do stavu ze shops.yaml.\n")
    path.write_text(header + yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")


def load_shops(path: Path = ROOT / "shops.yaml") -> list[Shop]:
    cfg = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    overrides = load_overrides()
    shops = []
    for raw in cfg.get("shops") or []:
        raw = dict(raw)
        raw.update(overrides.get(raw.get("id"), {}))
        raw["vat"] = float(raw["vat"])
        shops.append(Shop(**raw))
    ids = [s.id for s in shops]
    dup = {i for i in ids if ids.count(i) > 1}
    if dup:
        raise ValueError(f"Duplicate shop id(s) in shops.yaml: {sorted(dup)}")
    return shops


def load_skus(path: Path = ROOT / "skus.csv") -> list[Sku]:
    skus: list[Sku] = []
    with path.open(encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            if not row.get("sku_id") or row["sku_id"].startswith("#"):
                continue
            search = [x.strip() for x in (row.get("search") or "").split("|") if x.strip()]
            pn = (row.get("part_number") or "").strip()
            if pn:
                search.insert(0, pn)
            if not search:
                raise ValueError(f"SKU {row['sku_id']} has no search term")
            skus.append(
                Sku(
                    sku_id=row["sku_id"].strip(),
                    brand=row.get("brand", "").strip(),
                    series=row.get("series", "").strip(),
                    generation=row.get("generation", "").strip(),
                    category=row.get("category", "").strip(),
                    name=row.get("name", "").strip(),
                    unit=row.get("unit", "").strip(),
                    search=search,
                    must_match=_compile(_split(row.get("must_match"))),
                    must_not_match=_compile(_split(row.get("must_not_match"))),
                    prefer=_compile(_split(row.get("prefer"))),
                    part_number=pn,
                    notes=row.get("notes", "").strip(),
                    raw=row,
                )
            )
    ids = [s.sku_id for s in skus]
    dup = {i for i in ids if ids.count(i) > 1}
    if dup:
        raise ValueError(f"Duplicate sku_id(s) in skus.csv: {sorted(dup)}")
    return skus
