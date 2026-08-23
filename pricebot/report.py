"""Turn raw rows into the tables pushed to Google Sheets / CSV."""
from __future__ import annotations

import csv
from collections import defaultdict
from pathlib import Path

from .config import DATA, Shop, Sku

AVAIL_CZ = {"in_stock": "skladem", "limited": "omezeně", "preorder": "předobjednávka",
            "backorder": "na objednávku", "out": "vyprodáno", "unknown": "?"}
STATUS_CZ = {"ok": "OK", "blocked": "BLOKOVÁNO", "not_found": "nenalezeno", "parse_fail": "cena nenalezena",
             "error": "chyba", "http_error": "HTTP chyba"}


def _r(x, n=2):
    return round(x, n) if isinstance(x, (int, float)) else ""


def _pct(new: float | None, old: float | None) -> float | str:
    if not new or not old:
        return ""
    return round((new - old) / old * 100.0, 1)


def build(rows: list[dict], skus: list[Sku], shops: list[Shop], prev_rows: list[dict],
          min30: dict[str, float], settings: dict) -> dict:
    by_sku: dict[str, dict[str, dict]] = defaultdict(dict)
    for r in rows:
        if r["status"] == "ok" and r.get("price_eur_net"):
            by_sku[r["sku_id"]][r["shop_id"]] = r

    prev_min: dict[str, dict] = {}
    for r in prev_rows:
        if r.get("status") == "ok" and r.get("price_eur_net"):
            cur = prev_min.get(r["sku_id"])
            if cur is None or r["price_eur_net"] < cur["price_eur_net"]:
                prev_min[r["sku_id"]] = r

    shop_by_id = {s.id: s for s in shops}
    matrix = [["SKU", "Název", "Značka", "Řada", "Generace", "Kategorie", "Jednotka",
               "Min € bez DPH", "Nejlevnější shop", "Δ % vs. minulý běh", "Δ % vs. 30d min", "Dostupnost (min)"]
              + [s.name for s in shops]]
    minimum = [["SKU", "Název", "Min € bez DPH", "Min € s DPH", "Shop", "Země", "Cena v shopu", "Měna",
                "Dostupnost", "Δ % vs. minulý běh", "Δ % vs. 30d min", "Název v shopu", "URL", "Datum", "Poznámka"]]
    changes = [["Datum", "SKU", "Název", "Typ změny", "Nyní € bez DPH", "Shop", "Předtím € bez DPH", "Shop předtím", "Δ %", "URL"]]
    min_rows: list[dict] = []

    for sku in skus:
        offers = by_sku.get(sku.sku_id, {})
        best = min(offers.values(), key=lambda r: r["price_eur_net"]) if offers else None
        prev = prev_min.get(sku.sku_id)
        m30 = min30.get(sku.sku_id)
        d_prev = _pct(best["price_eur_net"], prev["price_eur_net"]) if best and prev else ""
        d_30 = _pct(best["price_eur_net"], m30) if best and m30 else ""

        line = [sku.sku_id, sku.name, sku.brand, sku.series, sku.generation, sku.category, sku.unit,
                _r(best["price_eur_net"]) if best else "",
                shop_by_id[best["shop_id"]].name if best else "",
                d_prev, d_30, AVAIL_CZ.get(best["availability"], "?") if best else ""]
        for s in shops:
            r = offers.get(s.id)
            line.append(_r(r["price_eur_net"]) if r else "")
        matrix.append(line)

        if best:
            note = []
            if best.get("flag"):
                note.append(best["flag"])
            minimum.append([sku.sku_id, sku.name, _r(best["price_eur_net"]), _r(best["price_eur"]),
                            shop_by_id[best["shop_id"]].name, shop_by_id[best["shop_id"]].country,
                            _r(best["price_local"]), best["currency"], AVAIL_CZ.get(best["availability"], "?"),
                            d_prev, d_30, best["title"], best["url"], best["date"], "; ".join(note)])
            min_rows.append({"date": best["date"], "sku_id": sku.sku_id, "price_eur_net": round(best["price_eur_net"], 2),
                             "shop_id": best["shop_id"], "price_local": best["price_local"],
                             "currency": best["currency"], "url": best["url"]})

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
                changes.append([best["date"], sku.sku_id, sku.name, ", ".join(kinds), _r(best["price_eur_net"]),
                                shop_by_id[best["shop_id"]].name,
                                _r(prev["price_eur_net"]) if prev else "",
                                shop_by_id[prev["shop_id"]].name if prev and prev["shop_id"] in shop_by_id else (prev["shop_id"] if prev else ""),
                                d_prev, best["url"]])
        else:
            minimum.append([sku.sku_id, sku.name, "", "", "", "", "", "", "", "", "", "", "", "", "žádný shop nenalezen"])

    detail = [["Datum", "SKU", "Shop", "Stav", "Název v shopu", "Cena v shopu", "Měna", "€ s DPH", "€ bez DPH",
               "DPH", "Dostupnost", "Zdroj", "Poznámka", "URL"]]
    for r in rows:
        detail.append([r["date"], r["sku_id"], shop_by_id.get(r["shop_id"], r["shop_id"]).name if r["shop_id"] in shop_by_id else r["shop_id"],
                       STATUS_CZ.get(r["status"], r["status"]), r.get("title", ""), _r(r.get("price_local")),
                       r.get("currency", ""), _r(r.get("price_eur")), _r(r.get("price_eur_net")),
                       f'{int(round(r["vat"] * 100))} %' if r.get("vat") is not None else "",
                       AVAIL_CZ.get(r.get("availability", "unknown"), "?"), r.get("source", ""), r.get("flag", ""), r.get("url", "")])

    return {"matrix": matrix, "minimum": minimum, "changes": changes, "detail": detail, "min_rows": min_rows}


def write_csvs(tables: dict, out_dir: Path = DATA) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    for name in ("matrix", "minimum", "changes", "detail"):
        with (out_dir / f"{name}.csv").open("w", encoding="utf-8", newline="") as fh:
            csv.writer(fh).writerows(tables[name])


def changes_markdown(tables: dict) -> str:
    ch = tables["changes"]
    if len(ch) <= 1:
        return "Žádné změny nad prahem.\n"
    lines = ["| " + " | ".join(ch[0][:9]) + " |", "|" + "---|" * 9]
    for row in ch[1:]:
        lines.append("| " + " | ".join(str(x) for x in row[:9]) + " |")
    return "\n".join(lines) + "\n"
