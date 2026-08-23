"""On-disk state kept in data/ (committed back to the repo by the GitHub Action)."""
from __future__ import annotations

import csv
import json
from datetime import date, timedelta
from pathlib import Path

from .config import DATA

URLS = DATA / "urls.json"
LATEST = DATA / "latest.json"
HISTORY_DIR = DATA / "history"
HISTORY_MIN = DATA / "history_min.csv"

ROW_FIELDS = [
    "date", "run_ts", "sku_id", "shop_id", "status", "title", "url",
    "price_local", "currency", "price_eur", "price_eur_net", "vat", "availability", "source", "flag",
]
MIN_FIELDS = ["date", "sku_id", "price_eur_net", "shop_id", "price_local", "currency", "url"]


# ---------------------------------------------------------------- url cache
def load_urls() -> dict:
    if URLS.exists():
        return json.loads(URLS.read_text(encoding="utf-8"))
    return {}


def save_urls(urls: dict) -> None:
    URLS.parent.mkdir(parents=True, exist_ok=True)
    URLS.write_text(json.dumps(urls, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")


def set_manual_url(sku_id: str, shop_id: str, url: str | None) -> None:
    urls = load_urls()
    if url:
        urls.setdefault(sku_id, {})[shop_id] = {"url": url, "manual": True, "resolved_at": date.today().isoformat()}
    else:
        urls.get(sku_id, {}).pop(shop_id, None)
    save_urls(urls)


# ---------------------------------------------------------------- runs
def load_latest() -> list[dict]:
    if LATEST.exists():
        return json.loads(LATEST.read_text(encoding="utf-8")).get("rows", [])
    return []


def save_latest(rows: list[dict], run_ts: str) -> None:
    LATEST.parent.mkdir(parents=True, exist_ok=True)
    LATEST.write_text(json.dumps({"run_ts": run_ts, "rows": rows}, ensure_ascii=False, indent=0), encoding="utf-8")


def append_history(rows: list[dict], today: date) -> Path:
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    path = HISTORY_DIR / f"{today:%Y-%m}.csv"
    new = not path.exists()
    with path.open("a", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=ROW_FIELDS, extrasaction="ignore")
        if new:
            w.writeheader()
        w.writerows(rows)
    return path


def append_history_min(min_rows: list[dict]) -> None:
    HISTORY_MIN.parent.mkdir(parents=True, exist_ok=True)
    new = not HISTORY_MIN.exists()
    with HISTORY_MIN.open("a", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=MIN_FIELDS, extrasaction="ignore")
        if new:
            w.writeheader()
        w.writerows(min_rows)


def min_over_days(days: int, today: date) -> dict[str, float]:
    """{sku_id: lowest net EUR in the last N days} from history_min.csv (excluding today)."""
    out: dict[str, float] = {}
    if not HISTORY_MIN.exists():
        return out
    since = (today - timedelta(days=days)).isoformat()
    with HISTORY_MIN.open(encoding="utf-8", newline="") as fh:
        for r in csv.DictReader(fh):
            if r["date"] < since or r["date"] >= today.isoformat():
                continue
            try:
                v = float(r["price_eur_net"])
            except (TypeError, ValueError):
                continue
            if r["sku_id"] not in out or v < out[r["sku_id"]]:
                out[r["sku_id"]] = v
    return out
