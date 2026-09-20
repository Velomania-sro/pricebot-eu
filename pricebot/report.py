"""Turn raw rows into the tables pushed to Google Sheets / CSV."""
from __future__ import annotations

import csv
from collections import defaultdict
from statistics import median
from pathlib import Path

from .config import DATA, Shop, Sku
from .fx import to_czk

AVAIL_CZ = {"in_stock": "skladem", "limited": "omezeně", "preorder": "předobjednávka",
            "backorder": "na objednávku", "supplier": "skladem u dodavatele", "out": "vyprodáno", "unknown": "?"}
STATUS_CZ = {"ok": "OK", "blocked": "BLOKOVÁNO", "not_found": "nenalezeno", "parse_fail": "cena nenalezena",
             "error": "chyba", "http_error": "HTTP chyba", "implausible": "podezřele nízká cena"}
CSV_NAMES = {"matrix": "matrix", "minimum": "minimum", "changes": "changes", "detail": "detail", "over": "nad-prahem"}


def _r(x, n=2):
    return round(x, n) if isinstance(x, (int, float)) else ""


def _czk_net(r: dict | None, rate: float | None) -> int | str:
    """Whole CZK net of VAT. Rows written before CZK output lack price_czk_net -> convert at today's rate."""
    if not r:
        return ""
    v = r.get("price_czk_net")
    if v is None:
        v = to_czk(r.get("price_eur_net"), rate)
    return "" if v is None else int(round(v))


def _pct(new: float | None, old: float | None) -> float | str:
    if not new or not old:
        return ""
    return round((new - old) / old * 100.0, 1)


IN_STOCK = ("in_stock", "limited", "supplier")


def buyable(r: dict, settings: dict) -> bool:
    """With in_stock_only an offer counts only when the shop has it on the shelf (unknown availability does not)."""
    return not settings.get("in_stock_only", False) or r.get("availability") in IN_STOCK


def _too_low(czk, ref: float | None, pct: float) -> bool:
    """More than pct % under the reference price -> not this product (a spare part with the same part number)."""
    return pct > 0 and ref is not None and isinstance(czk, (int, float)) and czk < ref * (1 - pct / 100.0) - 1e-9


def flag_implausible(rows: list[dict], supplier: dict | None, settings: dict, rate: float | None) -> list[dict]:
    """Turn 'ok' rows priced more than implausible_below_pct % under the reference into status 'implausible'.

    Reference = supplier price; without one, the median of the OTHER shops' offers (at least two of them,
    so a lone pair of offers never condemns each other). Flagged rows keep their price for the Detail sheet
    but, not being 'ok', drop out of Matice / Minimum / Změny / Nad prahem and out of the minimum history.
    Returns the flagged rows.
    """
    pct = float(settings.get("implausible_below_pct", 0) or 0)
    supplier = supplier or {}
    by_sku: dict[str, list[tuple[dict, int]]] = defaultdict(list)
    for r in rows:
        czk = _czk_net(r, rate)
        if r.get("status") == "ok" and isinstance(czk, int):
            by_sku[r["sku_id"]].append((r, czk))
    flagged: list[tuple[dict, int, float, str]] = []
    for sku_id, offers in by_sku.items():
        sup = (supplier.get(sku_id) or {}).get("net_czk")
        for r, czk in offers:
            others = [c for o, c in offers if o is not r]
            if sup is not None:
                ref, src = sup, "cena dodavatele"
            elif len(others) >= 2:
                ref, src = median(others), "medián ostatních shopů"
            else:
                continue
            if _too_low(czk, ref, pct):
                flagged.append((r, czk, ref, src))
    for r, czk, ref, src in flagged:                # až po vyhodnocení všech, ať se mediány nemění pod rukama
        r["status"] = "implausible"
        why = f"vyřazeno: {_pct(czk, ref)} % vs {src} {round(ref)} Kč – nejspíš jiný sortiment"
        r["flag"] = "; ".join(x for x in (r.get("flag", ""), why) if x)
    return [f[0] for f in flagged]


def _num(x: float) -> int | float:
    return int(x) if float(x).is_integer() else round(x, 2)


def _passes(czk, limit: float | None) -> bool:
    """Offer belongs to the main sheets: no supplier price, no CZK price to compare, or price <= limit."""
    return limit is None or not isinstance(czk, (int, float)) or czk <= limit + 1e-9


def build(rows: list[dict], skus: list[Sku], shops: list[Shop], prev_rows: list[dict],
          min30: dict[str, float], settings: dict, fx: dict | None = None, supplier: dict | None = None) -> dict:
    """Shown prices are CZK net of VAT; ranking and Δ % stay on price_eur_net so they compare with history.

    fx = {"rate": CZK per 1 EUR, "date": day the rate is valid for}.
    supplier = {sku_id: {"net_czk": ...}} (config.load_supplier). An offer of a SKU with a supplier price
    is shown in Matice / Minimum / Změny only when it costs at most supplier * (1 + supplier_threshold_pct %);
    the rest goes to "over" (list Nad prahem). min_rows (history) always keeps the real market minimum.
    """
    rate = (fx or {}).get("rate")
    fx_date = (fx or {}).get("date") or ("záložní kurz" if rate else "")
    supplier = supplier or {}
    sup_thr = float(settings.get("supplier_threshold_pct", 10))
    low_pct = float(settings.get("implausible_below_pct", 0) or 0)
    prev_by_sku: dict[str, list[dict]] = defaultdict(list)
    for r in prev_rows:
        if r.get("status") == "ok" and r.get("price_eur_net") and buyable(r, settings):
            prev_by_sku[r["sku_id"]].append(r)

    shop_by_id = {s.id: s for s in shops}
    by_sku = defaultdict(dict)
    off_shelf = defaultdict(list)
    for r in rows:
        if r["shop_id"] not in shop_by_id:            # a shop switched off since the run: no column, no vote
            continue
        if r["status"] == "ok" and r.get("price_eur_net"):
            if buyable(r, settings):
                by_sku[r["sku_id"]][r["shop_id"]] = r
            else:
                off_shelf[r["sku_id"]].append(r)
    matrix_head = (["SKU", "Název", "Značka", "Řada", "Generace", "Kategorie", "Jednotka",
                    "Min Kč bez DPH", "Nejlevnější shop", "Dodavatel CZK bez DPH", "Δ % vs dodavatel",
                    "Δ % vs. minulý běh", "Δ % vs. 30d min", "Dostupnost (min)"] + [s.name for s in shops])
    min_head = ["SKU", "Název", "Min Kč bez DPH", "Min Kč s DPH", "Dodavatel CZK bez DPH", "Δ % vs dodavatel",
                "Úspora CZK", "Shop", "Země", "Cena v shopu", "Měna", "Dostupnost", "Δ % vs. minulý běh",
                "Δ % vs. 30d min", "Název v shopu", "URL", "Datum", "Poznámka"]
    minimum = [min_head]
    changes = [["Datum", "SKU", "Název", "Typ změny", "Nyní Kč bez DPH", "Shop", "Předtím Kč bez DPH", "Shop předtím", "Δ %", "URL"]]
    matrix_lines: list[tuple[float, list]] = []     # (úspora proti dodavateli, řádek)
    over_lines: list[tuple[float, list]] = []       # (Δ % vs dodavatel, řádek)
    min_rows: list[dict] = []

    def offer_line(sku: Sku, r: dict, sup_czk, d_prev, d_30, note: list[str]) -> list:
        czk = _czk_net(r, rate)
        has = sup_czk is not None and isinstance(czk, int)
        return [sku.sku_id, sku.name, czk, _r(to_czk(r.get("price_eur"), rate), 0),
                _num(sup_czk) if sup_czk is not None else "", _pct(czk, sup_czk) if has else "",
                int(round(sup_czk - czk)) if has else "",
                shop_by_id[r["shop_id"]].name, shop_by_id[r["shop_id"]].country,
                _r(r["price_local"]), r["currency"], AVAIL_CZ.get(r["availability"], "?"),
                d_prev, d_30, r["title"], r["url"], r["date"], "; ".join(n for n in note if n)]

    for sku in skus:
        offers = by_sku.get(sku.sku_id, {})
        market_best = min(offers.values(), key=lambda r: r["price_eur_net"]) if offers else None
        sup_czk = (supplier.get(sku.sku_id) or {}).get("net_czk")
        limit = sup_czk * (1 + sup_thr / 100.0) if sup_czk is not None else None
        shown = {sid: r for sid, r in offers.items() if _passes(_czk_net(r, rate), limit)}
        best = min(shown.values(), key=lambda r: r["price_eur_net"]) if shown else None
        # Older runs stored mismatched spare parts as the minimum; don't measure today's price against those.
        now_czk = [c for c in (_czk_net(r, rate) for r in offers.values()) if isinstance(c, int)]
        ref = sup_czk if sup_czk is not None else (median(now_czk) if len(now_czk) >= 3 else None)
        prev_ok = [r for r in prev_by_sku.get(sku.sku_id, []) if not _too_low(_czk_net(r, rate), ref, low_pct)]
        prev = min(prev_ok, key=lambda r: r["price_eur_net"]) if prev_ok else None
        m30 = min30.get(sku.sku_id)
        if m30 and _too_low(to_czk(m30, rate), ref, low_pct):
            m30 = None
        d_prev = _pct(market_best["price_eur_net"], prev["price_eur_net"]) if market_best and prev else ""
        d_30 = _pct(market_best["price_eur_net"], m30) if market_best and m30 else ""
        sup_note = "" if sup_czk is not None else "bez ceny dodavatele"

        best_czk = _czk_net(best, rate)
        saving = sup_czk - best_czk if sup_czk is not None and isinstance(best_czk, int) else None
        line = [sku.sku_id, sku.name, sku.brand, sku.series, sku.generation, sku.category, sku.unit,
                best_czk, shop_by_id[best["shop_id"]].name if best else "",
                _num(sup_czk) if sup_czk is not None else "", _pct(best_czk, sup_czk) if saving is not None else "",
                d_prev if best else "", d_30 if best else "", AVAIL_CZ.get(best["availability"], "?") if best else ""]
        line += [_czk_net(shown.get(s.id), rate) for s in shops]
        matrix_lines.append((saving if saving is not None else float("-inf"), line))

        for r in offers.values():
            if r["shop_id"] not in shown:
                is_min = r is market_best
                ol = offer_line(sku, r, sup_czk, d_prev if is_min else "", d_30 if is_min else "", [r.get("flag", "")])
                over_lines.append((ol[5], ol))

        if market_best:                             # historie = skutečné tržní minimum, bez ohledu na práh
            min_rows.append({"date": market_best["date"], "sku_id": sku.sku_id,
                             "price_eur_net": round(market_best["price_eur_net"], 2),
                             "shop_id": market_best["shop_id"], "price_local": market_best["price_local"],
                             "currency": market_best["currency"], "url": market_best["url"]})

        if best:
            minimum.append(offer_line(sku, best, sup_czk, d_prev, d_30, [best.get("flag", ""), sup_note]))

            thr = float(settings["alert_pct"])
            kinds = []
            if prev and isinstance(d_prev, float) and abs(d_prev) >= thr:
                kinds.append("pokles ceny" if d_prev < 0 else "zdražení")
            if prev and prev["shop_id"] != best["shop_id"]:
                kinds.append("nový nejlevnější shop")
            if m30 and best["price_eur_net"] < m30 - 0.005:
                kinds.append("nové 30denní minimum")
            if not prev and not m30:
                kinds.append("první záznam")
            if kinds:
                changes.append([best["date"], sku.sku_id, sku.name, ", ".join(kinds), _czk_net(best, rate),
                                shop_by_id[best["shop_id"]].name,
                                _czk_net(prev, rate),
                                shop_by_id[prev["shop_id"]].name if prev and prev["shop_id"] in shop_by_id else (prev["shop_id"] if prev else ""),
                                d_prev, best["url"]])
        else:
            if market_best:
                closest = _pct(_czk_net(market_best, rate), sup_czk)
                why = f"vše nad prahem +{_num(sup_thr)} % (nejblíž {shop_by_id[market_best['shop_id']].name}: +{closest} %)"
            elif off_shelf.get(sku.sku_id):
                gone = min(off_shelf[sku.sku_id], key=lambda r: r["price_eur_net"])
                why = "; ".join(n for n in (
                    f"nic skladem ({len(off_shelf[sku.sku_id])} nabídek mimo sklad, nejlevnější {_czk_net(gone, rate)} Kč "
                    f"u {shop_by_id[gone['shop_id']].name}: {AVAIL_CZ.get(gone['availability'], '?')})", sup_note) if n)
            else:
                why = "; ".join(n for n in ("žádný shop nenalezen", sup_note) if n)
            empty = [""] * len(min_head)
            empty[0], empty[1], empty[-1] = sku.sku_id, sku.name, why
            empty[4] = _num(sup_czk) if sup_czk is not None else ""
            minimum.append(empty)

    # Matice: největší úspora proti dodavateli nahoře; SKU bez úspory (bez ceny dodavatele / bez nabídky) pod nimi
    matrix = [matrix_head] + [ln for _, ln in sorted(matrix_lines, key=lambda t: -t[0])]
    over = [["SKU", "Název", "Kč bez DPH", "Kč s DPH"] + min_head[4:]]
    over += [ln for _, ln in sorted(over_lines, key=lambda t: t[0])]

    for line in minimum[1:] + over[1:]:
        line += [_r(rate, 3), fx_date]
    for head in (minimum[0], over[0]):
        head += ["Kurz CZK/EUR", "Kurz k datu"]

    detail = [["Datum", "SKU", "Shop", "Stav", "Název v shopu", "Cena v shopu", "Měna", "Kč bez DPH", "€ bez DPH", "€ s DPH",
               "DPH", "Dostupnost", "Zdroj", "Poznámka", "URL"]]
    for r in rows:
        detail.append([r["date"], r["sku_id"], shop_by_id.get(r["shop_id"], r["shop_id"]).name if r["shop_id"] in shop_by_id else r["shop_id"],
                       STATUS_CZ.get(r["status"], r["status"]), r.get("title", ""), _r(r.get("price_local")),
                       r.get("currency", ""), _czk_net(r, rate), _r(r.get("price_eur_net")), _r(r.get("price_eur")),
                       f'{int(round(r["vat"] * 100))} %' if r.get("vat") is not None else "",
                       AVAIL_CZ.get(r.get("availability", "unknown"), "?"), r.get("source", ""), r.get("flag", ""), r.get("url", "")])

    return {"matrix": matrix, "minimum": minimum, "changes": changes, "detail": detail, "over": over,
            "min_rows": min_rows}


def write_csvs(tables: dict, out_dir: Path = DATA) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, fname in CSV_NAMES.items():
        with (out_dir / f"{fname}.csv").open("w", encoding="utf-8", newline="") as fh:
            csv.writer(fh).writerows(tables[name])


def changes_markdown(tables: dict) -> str:
    ch = tables["changes"]
    if len(ch) <= 1:
        return "Žádné změny nad prahem.\n"
    lines = ["| " + " | ".join(ch[0][:9]) + " |", "|" + "---|" * 9]
    for row in ch[1:]:
        lines.append("| " + " | ".join(str(x) for x in row[:9]) + " |")
    return "\n".join(lines) + "\n"
