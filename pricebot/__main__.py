"""CLI entry point.

  python -m pricebot run   [--shops a,b] [--skus X,Y] [--limit N] [--no-sheet] [--verbose]
  python -m pricebot probe [--shops a,b] [--q RD-R8150]
  python -m pricebot set-url SKU_ID SHOP_ID URL      (manual override; URL "-" removes it)
  python -m pricebot export                          (rebuild CSV tables from data/latest.json)
"""
from __future__ import annotations

import argparse
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace
from datetime import date, datetime, timezone
from urllib.parse import quote_plus

from . import claude_fallback, report, sheet, sitemap, store
from .config import DATA, Shop, Sku, load_overrides, load_settings, load_shops, load_skus, save_overrides
from .fetch import Blocked, Fetcher
from .fx import get_rates, to_eur
from .parse import parse_product_page, search_candidates
from .resolve import pick, prefer_score, resolve

_lock = threading.Lock()


def log(msg: str) -> None:
    with _lock:
        print(msg, flush=True)


# ---------------------------------------------------------------- run
def _row(sku: Sku, shop: Shop, today: str, ts: str, status: str, **kw) -> dict:
    r = {"date": today, "run_ts": ts, "sku_id": sku.sku_id, "shop_id": shop.id, "status": status,
         "title": "", "url": "", "price_local": None, "currency": "", "price_eur": None, "price_eur_net": None,
         "vat": shop.vat, "availability": "unknown", "source": "", "flag": ""}
    r.update(kw)
    return r


def _ok_row(sku, shop, today, ts, offer, url, rates) -> dict:
    cur = (offer.currency or shop.currency).upper()
    note = ""
    if cur not in rates:                      # neznámý/chybějící kód měny -> ber měnu shopu
        note = f"neznámá měna '{offer.currency}', počítám v {shop.currency}"
        cur = shop.currency.upper()
    eur = to_eur(offer.price, cur, rates)
    if eur is None:
        return _row(sku, shop, today, ts, "error", title=offer.name, url=url, flag=f"neznámá měna {cur}")
    flags = [note] if note else []
    if cur != shop.currency.upper():
        flags.append(f"jiná měna než čekáme ({cur} místo {shop.currency}) – ověř, zda cena obsahuje DPH")
    if sku.prefer and prefer_score(offer.name, sku) == 0:
        flags.append("ověřit variantu")
    if offer.availability == "out":
        flags.append("vyprodáno")
    if offer.source == "claude":
        flags.append("cena z LLM")
    return _row(sku, shop, today, ts, "ok", title=offer.name, url=url, price_local=round(offer.price, 2), currency=cur,
                price_eur=round(eur, 2), price_eur_net=round(eur / (1 + shop.vat), 2),
                availability=offer.availability, source=offer.source, flag="; ".join(flags))


def process_shop(shop: Shop, skus: list[Sku], urls: dict, rates: dict, settings: dict, today: str, ts: str,
                 verbose: bool) -> tuple[list[dict], dict]:
    fetcher = Fetcher(shop, settings)
    rows: list[dict] = []
    updates: dict[str, dict] = {}
    blocks = 0
    model = settings["claude_model"]
    extra = (lambda html, url, sku: claude_fallback.claude_extract(html, url, sku, model)) if claude_fallback.enabled() else None
    t0 = time.monotonic()
    try:
        for sku in skus:
            if blocks >= int(settings["max_blocks_per_shop"]):
                rows.append(_row(sku, shop, today, ts, "blocked"))
                continue
            entry = (urls.get(sku.sku_id) or {}).get(shop.id)
            try:
                offer = url = None
                if entry and entry.get("url"):
                    page = fetcher.get(entry["url"])
                    if page.status < 400:
                        offer = pick(parse_product_page(page.text, page.url), sku, shop.currency, shop.country)
                        if offer is None and extra is not None:
                            o = extra(page.text, page.url, sku)
                            offer = o if o is not None and pick([o], sku) else None
                        url = page.url
                    if offer is None:
                        if entry.get("manual"):
                            rows.append(_row(sku, shop, today, ts, "parse_fail" if page.status < 400 else "http_error",
                                             url=entry["url"], flag=f"HTTP {page.status}"))
                            continue
                        if verbose:
                            log(f"[{shop.id}] {sku.sku_id}: cached URL no longer valid, re-resolving")
                if offer is None:
                    found = resolve(sku, shop, fetcher, settings, log if verbose else (lambda m: None), extra)
                    if not found:
                        rows.append(_row(sku, shop, today, ts, "not_found"))
                        continue
                    offer, url = found
                    updates[sku.sku_id] = {"url": url, "title": offer.name, "manual": False, "resolved_at": today}
                rows.append(_ok_row(sku, shop, today, ts, offer, url, rates))
                if verbose:
                    r = rows[-1]
                    log(f"[{shop.id}] {sku.sku_id}: {r['price_local']} {r['currency']} -> {r['price_eur_net']} € net | {r['title'][:60]}")
            except Blocked as exc:
                blocks += 1
                log(f"[{shop.id}] BLOCKED ({blocks}): {exc}")
                rows.append(_row(sku, shop, today, ts, "blocked"))
            except Exception as exc:  # keep going, one SKU must not kill the shop
                log(f"[{shop.id}] {sku.sku_id}: ERROR {exc!r}")
                rows.append(_row(sku, shop, today, ts, "error", flag=repr(exc)[:120]))
    finally:
        fetcher.close()
    ok = sum(1 for r in rows if r["status"] == "ok")
    log(f"[{shop.id}] done: {ok}/{len(rows)} SKU with price, {time.monotonic() - t0:.0f}s")
    return rows, updates


def cmd_run(args) -> int:
    settings = load_settings()
    shops = [s for s in load_shops() if s.enabled and (not args.shops or s.id in args.shops)]
    skus = [k for k in load_skus() if not args.skus or k.sku_id in args.skus]
    if args.limit:
        skus = skus[: args.limit]
    if not shops or not skus:
        log("Nothing to do (no enabled shops or no SKUs selected).")
        return 1

    today = date.today().isoformat()
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    rates = get_rates()
    urls = store.load_urls()
    prev_rows = store.load_latest()
    log(f"Run {ts}: {len(skus)} SKU x {len(shops)} shops; FX CZK={rates.get('CZK')}")

    rows: list[dict] = []
    with ThreadPoolExecutor(max_workers=int(settings["parallel_shops"])) as ex:
        futs = {ex.submit(process_shop, s, skus, urls, rates, settings, today, ts, args.verbose): s for s in shops}
        for fut in as_completed(futs):
            shop = futs[fut]
            try:
                shop_rows, updates = fut.result()
            except Exception as exc:
                log(f"[{shop.id}] worker crashed: {exc!r}")
                continue
            rows.extend(shop_rows)
            for sku_id, entry in updates.items():
                urls.setdefault(sku_id, {})[shop.id] = entry

    store.save_urls(urls)
    store.append_history(rows, date.today())
    min30 = store.min_over_days(int(settings["history_days_for_min"]), date.today())
    tables = report.build(rows, skus, shops, prev_rows, min30, settings)
    store.save_latest(rows, ts)
    store.append_history_min(tables["min_rows"])
    report.write_csvs(tables, DATA)
    (DATA / "changes.md").write_text(report.changes_markdown(tables), encoding="utf-8")

    ok = sum(1 for r in rows if r["status"] == "ok")
    blocked = sum(1 for r in rows if r["status"] == "blocked")
    log(f"Summary: {ok} prices, {blocked} blocked, {len(tables['changes']) - 1} changes above threshold.")
    if not args.no_sheet:
        sheet.push(tables, settings, log)
    return 0


# ---------------------------------------------------------------- probe
def _try_template(shop: Shop, tpl: str, q: str, key_re, dummy: Sku, settings: dict, fetcher_kind: str) -> dict:
    """One (template, fetcher) attempt. Returns a verdict dict; never raises."""
    probe_shop = replace(shop, search_url=tpl, fetcher=fetcher_kind)
    f = Fetcher(probe_shop, settings)
    url = tpl.replace("{q}", quote_plus(q))
    out = {"tpl": tpl, "fetcher": fetcher_kind, "url": url, "status": None,
           "candidates": [], "price": None, "verdict": "", "ok": False, "blocked": False}
    try:
        page = f.get(url)
        out["status"] = page.status
        direct = pick(parse_product_page(page.text, page.url), dummy)
        if direct:
            out.update(price=f"{direct.price} {direct.currency}", verdict="vyhledávání vede rovnou na produkt", ok=True)
            return out
        cands = search_candidates(page.text, page.url, probe_shop.url_re, key_re)
        out["candidates"] = cands
        if not cands:
            out["verdict"] = "0 odkazů na produkt (špatná šablona, JS výpis, nebo chybí product_url_pattern)"
            return out
        p = f.get(cands[0][0])
        offers = parse_product_page(p.text, p.url)
        if offers:
            o = offers[0]
            out.update(price=f"{o.price} {o.currency}", verdict=f"OK ({len(cands)} kandidátů, cena ze zdroje '{o.source}')", ok=True)
        else:
            out["verdict"] = f"{len(cands)} kandidátů, ale na produktu není strukturovaná cena (JSON-LD/meta)"
    except Blocked as exc:
        out.update(verdict=f"BLOKOVÁNO ({exc})", blocked=True)
    except Exception as exc:
        out["verdict"] = f"CHYBA {exc!r}"
    finally:
        f.close()
    return out


def cmd_probe(args) -> int:
    """Try every candidate search template per shop, report what works, optionally persist the winner."""
    settings = load_settings()
    shops = [s for s in load_shops() if (not args.shops or s.id in args.shops)]
    q = args.q or "RD-R8150"
    key_re = re.compile(re.escape(q), re.I)
    dummy = Sku("probe", "", "", "", "", q, "", [q], [key_re], [], [])
    dummy_rd = next((s for s in load_skus() if s.sku_id == "SH-ULT-RD"), dummy)
    overrides = load_overrides()
    winners: dict[str, dict] = {}

    for shop in shops:
        print(f"\n=== [{shop.id}] {shop.name} " + "=" * max(0, 46 - len(shop.id) - len(shop.name)))
        best = None
        for tpl in shop.search_urls:
            kinds = [shop.fetcher] if shop.fetcher != "requests" else ["requests"]
            for kind in kinds:
                r = _try_template(shop, tpl, q, key_re, dummy, settings, kind)
                # Bot wall on plain requests? Playwright often walks straight through it.
                if r["blocked"] and kind == "requests" and not args.no_playwright:
                    print(f"  [requests] {r['verdict']}\n  ... zkouším Playwright")
                    r = _try_template(shop, tpl, q, key_re, dummy, settings, "playwright")
                mark = "OK  " if r["ok"] else "-   "
                print(f"  {mark}[{r['fetcher']}] {r['verdict']}")
                print(f"       {r['url']}")
                for u, t in r["candidates"][:3]:
                    print(f"         - {t[:64]!r} -> {u}")
                if r["price"]:
                    print(f"       cena: {r['price']}")
                if r["ok"] and best is None:
                    best = r
                    break
            if best:
                break
        if best:
            winners[shop.id] = {"search_url": best["tpl"], "fetcher": best["fetcher"], "verified": True}
            print(f"  => funguje: {best['tpl']}  (fetcher: {best['fetcher']})")
        else:
            print("  => vyhledávání nepoužitelné; zkouším sitemapu")
            f = Fetcher(shop, settings)
            try:
                urls = sitemap.collect_urls(shop, f.session, settings, log=lambda m: print("     " + m))
                hits = sitemap.candidates(dummy_rd, urls)
                if hits:
                    print(f"  OK  sitemapa: {len(urls)} URL, nalezeno {len(hits)}× RD-R8150, např.:")
                    for u, _ in hits[:2]:
                        print(f"         {u}")
                    print("  => shop pojede přes sitemapu (pomalejší první běh, pak z cache)")
                else:
                    print(f"  =>  sitemapa má {len(urls)} URL, ale RD-R8150 v nich není – viz README kap. 2")
            except Exception as exc:
                print(f"  =>  sitemapa selhala: {exc!r} – viz README kap. 2")
            finally:
                f.close()

    if winners and args.fix:
        for sid, vals in winners.items():
            overrides.setdefault(sid, {}).update(vals)
        save_overrides(overrides)
        print(f"\nZapsáno do shops.local.yaml pro {len(winners)} shopů (shops.yaml zůstal beze změny).")
    elif winners:
        print("\nSpusť znovu s --fix, ať se funkční šablony uloží do shops.local.yaml.")
    return 0


# ---------------------------------------------------------------- misc
def cmd_set_url(args) -> int:
    store.set_manual_url(args.sku_id, args.shop_id, None if args.url == "-" else args.url)
    print("OK")
    return 0


def cmd_export(args) -> int:
    settings = load_settings()
    shops = [s for s in load_shops() if s.enabled]
    skus = load_skus()
    rows = store.load_latest()
    tables = report.build(rows, skus, shops, [], {}, settings)
    report.write_csvs(tables, DATA)
    print(f"Exported {len(tables['matrix']) - 1} SKU rows to {DATA}/*.csv")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="pricebot")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("run")
    p.add_argument("--shops", type=lambda s: s.split(","), default=None)
    p.add_argument("--skus", type=lambda s: s.split(","), default=None)
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--no-sheet", action="store_true")
    p.add_argument("--verbose", "-v", action="store_true")
    p.set_defaults(fn=cmd_run)

    p = sub.add_parser("probe")
    p.add_argument("--shops", type=lambda s: s.split(","), default=None)
    p.add_argument("--q", default=None, help="hledaný výraz (výchozí RD-R8150)")
    p.add_argument("--fix", action="store_true", help="uložit funkční šablony do shops.local.yaml")
    p.add_argument("--no-playwright", action="store_true", help="nezkoušet Playwright při blokaci")
    p.set_defaults(fn=cmd_probe)

    p = sub.add_parser("set-url")
    p.add_argument("sku_id")
    p.add_argument("shop_id")
    p.add_argument("url")
    p.set_defaults(fn=cmd_set_url)

    p = sub.add_parser("export")
    p.set_defaults(fn=cmd_export)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
