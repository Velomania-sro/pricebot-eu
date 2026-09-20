"""Static HTML dashboard (one self-contained file) built from the rows of a run.

Follows the design handoff: every number and label is formatted here, in Python; the inline script only
switches views, filters rows, expands details, jumps from the side panel and mirrors that state in the URL hash.
The page shows supplier purchase prices -> it is written to data/dashboard.html, which is git-ignored.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from html import escape
from pathlib import Path
from statistics import median

from .config import DATA, Shop, Sku
from .fx import to_czk
from .report import AVAIL_CZ, STATUS_CZ, _czk_net, _too_low

RADA_ORDER = ["105 Di2", "Ultegra Di2", "Dura-Ace Di2", "Di2 společné", "Red AXS", "Force AXS", "Rival AXS"]
STATUS_ORDER = ["cheaper", "near", "over", "susp", "nosup", "none"]
BADGE = {"cheaper": "▼ levněji", "near": "≈ do +{p} %", "over": "▲ nad prahem", "susp": "? jen podezřelé",
         "nosup": "– bez ceny dodavatele", "none": "∅ žádná nabídka"}
AVAIL = {"in_stock": ("● skladem", "cheaper"), "limited": ("◐ omezeně", "near"), "backorder": ("○ na objednávku", "neutral"),
         "preorder": ("○ předobjednávka", "neutral"), "out": ("× vyprodáno", "over"), "unknown": ("? dostupnost", "neutral")}
CHANGE_ICONS = [("nové 30denní minimum", "↓30", "cheaper"), ("pokles ceny", "▼", "cheaper"), ("zdražení", "▲", "over"),
                ("nový nejlevnější shop", "★", "accent"), ("první záznam", "＋", "neutral")]
MINUS = "−"


# ---------------------------------------------------------------- formatting (cs-CZ)
def fmt_n(v) -> str:
    return "" if v is None else f"{round(v):,}".replace(",", " ").replace("-", MINUS)


def fmt_kc(v) -> str:
    return "—" if v is None else f"{fmt_n(v)} Kč"


def fmt_pct(p) -> str:
    if p is None:
        return ""
    r = round(p, 1)
    sign = "+" if r > 0 else MINUS if r < 0 else "±"
    return f"{sign}{abs(r):.1f} %".replace(".", ",")


def fmt_shop_price(v, cur: str) -> str:
    if v is None or v == "":
        return ""
    if cur == "CZK":
        return fmt_kc(v)
    num = f"{v:,.2f}".replace(",", " ").replace(".", ",")
    return f"{num} {'€' if cur == 'EUR' else cur}"


def fmt_rate(v) -> str:
    return "—" if not v else f"{v:.3f}".replace(".", ",")


def heat(pct: float, threshold: float) -> tuple[str, str]:
    """Matrix cell background / text colour for an offer priced pct % against the supplier."""
    if pct < 0:
        k = min(abs(pct), 40) / 40
        return f"oklch({0.97 - k * 0.17:.3f} {0.04 + k * 0.10:.3f} 150)", "#0f2a1a" if abs(pct) > 25 else "#1a1c1e"
    return ("oklch(0.95 0.07 85)", "#1a1c1e") if pct <= threshold else ("oklch(0.94 0.045 22)", "#1a1c1e")


# ---------------------------------------------------------------- context
def _kind(pct, threshold: float) -> str:
    return "nosup" if pct is None else "cheaper" if pct < 0 else "near" if pct <= threshold else "over"


def _notes(flag: str) -> list[str]:
    return [f for f in (flag or "").split("; ") if f]


def build_context(rows: list[dict], skus: list[Sku], shops: list[Shop], settings: dict, fx: dict | None,
                  supplier: dict | None, changes: list[list] | None = None,
                  history: dict[str, list[tuple[str, float]]] | None = None, generated: str | None = None) -> dict:
    """Everything the page shows, already sorted and formatted. rows = raw run rows (after flag_implausible)."""
    rate = (fx or {}).get("rate")
    supplier = supplier or {}
    threshold = float(settings.get("supplier_threshold_pct", 10))
    low_pct = float(settings.get("implausible_below_pct", 0) or 0)
    known = {s.sku_id for s in skus}
    rows = [r for r in rows if r["sku_id"] in known]        # latest.json may still carry SKUs dropped from skus.csv
    by_pair = {(r["sku_id"], r["shop_id"]): r for r in rows}
    shop_rows: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        shop_rows[r["shop_id"]].append(r)

    sku_ctx = []
    for sku in skus:
        sup = (supplier.get(sku.sku_id) or {}).get("net_czk")
        offers = []
        for shop in shops:
            r = by_pair.get((sku.sku_id, shop.id))
            o = {"shop": shop.name, "kc": None, "pct": None, "r": r}
            czk = _czk_net(r, rate)
            if r is None:
                o.update(kind="nodata", stav="∅ shop dnes neprošel")
            elif r["status"] == "blocked":
                o.update(kind="blocked", stav="⊘ blokováno")
            elif r["status"] in ("ok", "implausible") and isinstance(czk, int):
                pct = (czk - sup) / sup * 100 if sup else None
                susp = r["status"] == "implausible"
                o.update(kind="susp" if susp else _kind(pct, threshold), kc=czk, pct=pct,
                         stav="? podezřelá shoda" if susp else "OK" + (f" · {r['source']}" if r.get("source") else ""))
            else:
                o.update(kind="missing", stav="· " + STATUS_CZ.get(r["status"], r["status"]).lower())
            offers.append(o)

        valid = sorted((o for o in offers if o["kc"] is not None and o["kind"] != "susp"), key=lambda o: o["kc"])
        susp = [o for o in offers if o["kind"] == "susp"]
        best = valid[0] if valid else None
        pct = (best["kc"] - sup) / sup * 100 if best and sup else None
        status = ("susp" if susp else "none") if not best else _kind(pct, threshold)
        saving = sup - best["kc"] if best and sup is not None else None

        tags = []
        if best:
            label, tone = AVAIL.get(best["r"].get("availability", "unknown"), AVAIL["unknown"])
            tags.append({"label": label, "tone": tone, "title": f"Dostupnost u {best['shop']}"})
            for note in _notes(best["r"].get("flag", "")):
                if note not in ("vyprodáno",):
                    tags.append({"label": "⚠ " + (note if len(note) < 28 else note[:26] + "…"), "tone": "near",
                                 "title": f"{note} · název v shopu: {best['r'].get('title', '')}"})
        if susp:
            tags.append({"label": f"? {len(susp)} podezř.", "tone": "susp",
                         "title": "\n".join(f"{o['shop']}: {fmt_kc(o['kc'])} — {o['r'].get('title', '')}" for o in susp)})

        badge = BADGE[status].replace("{p}", fmt_n(threshold))
        badge_sub = f"nejblíž {fmt_pct(pct)}" if status == "over" else ""
        if pct is None:
            diff_kc = "bez srovnání" if best else ""
        else:
            diff_kc = (f"o {fmt_kc(sup - best['kc'])} levněji" if round(sup - best["kc"]) > 0
                       else f"o {fmt_kc(best['kc'] - sup)} dráž" if round(best["kc"] - sup) > 0 else "stejná cena")

        detail = []
        for i, o in enumerate(valid + susp + [o for o in offers if o["kc"] is None]):
            r = o["r"] or {}
            reasons = _notes(r.get("flag", ""))
            detail.append({
                "rank": f"{i + 1}. " if o in valid else "", "shop": o["shop"], "best": o is best, "kind": o["kind"],
                "name": r.get("title", "") if o["kc"] is not None else "",
                "price_shop": fmt_shop_price(r.get("price_local"), r.get("currency", "")) if o["kc"] is not None else "",
                "kc": "" if o["kc"] is None else fmt_kc(o["kc"]),
                "dph": f"{round(r['vat'] * 100)} %" if r.get("vat") is not None else "",
                "pct": ("? " if o["kind"] == "susp" else "") + fmt_pct(o["pct"]),
                "avail": AVAIL.get(r.get("availability", "unknown"), AVAIL["unknown"])[0] if o["kc"] is not None else "",
                "stav": o["stav"], "note": "; ".join(reasons), "url": r.get("url", "") if o["kc"] is not None else "",
            })

        cells = []
        for o in offers:
            is_min = o is best
            c = {"ring": is_min, "bold": is_min, "bg": "", "fg": ""}
            if o["kind"] == "nodata":
                c.update(text="∅", cls="faint", title=f"{o['shop']}: shop dnes neprošel")
            elif o["kind"] == "blocked":
                c.update(text="⊘", cls="blocked", title=f"{o['shop']}: blokováno")
            elif o["kind"] == "missing":
                c.update(text="·", cls="faint", title=f"{o['shop']}: {o['stav'][2:]}")
            elif o["kind"] == "susp":
                c.update(text="? " + fmt_n(o["kc"]), cls="susp",
                         title=f"{o['shop']}: podezřelá shoda — {o['r'].get('title', '')}")
            elif o["kind"] == "nosup":
                c.update(text=fmt_n(o["kc"]), cls="nosup", title=f"{o['shop']}: {fmt_kc(o['kc'])} (bez ceny dodavatele)")
            else:
                bg, fg = heat(o["pct"], threshold)
                extra = "".join(f" · {n}" for n in _notes(o["r"].get("flag", "")))
                c.update(text=fmt_n(o["kc"]), cls="", bg=bg, fg=fg,
                         title=f"{o['shop']}: {fmt_kc(o['kc'])} · {fmt_pct(o['pct'])} vs dodavatel · "
                               f"{AVAIL_CZ.get(o['r'].get('availability', 'unknown'), '?')}{extra}")
            cells.append(c)

        ref = sup if sup is not None else (median(o["kc"] for o in valid) if len(valid) >= 3 else None)
        series = [(d, to_czk(eur, rate)) for d, eur in (history or {}).get(sku.sku_id, [])]
        series = [(d, v) for d, v in series if v is not None and not _too_low(v, ref, low_pct)]

        sku_ctx.append({
            "sku": sku.sku_id, "name": sku.name, "rada": sku.series, "kategorie": sku.category, "status": status,
            "badge": badge, "badge_sub": badge_sub, "sup": sup, "sup_text": fmt_kc(sup), "sup_short": "—" if sup is None else fmt_n(sup),
            "best": best, "pct": pct, "saving": saving,
            "min_text": fmt_kc(best["kc"]) if best else ("jen podezřelé shody" if status == "susp" else "—"),
            "min_shop": best["shop"] if best else (f"{len(susp)} nabídek k ověření" if status == "susp" else ""),
            "diff_pct": fmt_pct(pct) if pct is not None else ("—" if best else ""), "diff_kc": diff_kc,
            "diff_tone": _kind(pct, threshold) if pct is not None else "muted",
            "in_stock": bool(best and best["r"].get("availability") == "in_stock"),
            "tags": tags, "url": best["r"].get("url", "") if best else "",
            "detail_note": (f"dodavatel {fmt_kc(sup)} · " if sup is not None else "bez ceny dodavatele · ")
                           + f"{len(valid)} platných nabídek" + (f", {len(susp)} podezřelých" if susp else ""),
            "detail": detail, "cells": cells, "series": series, "susp": susp,
        })

    order = [n for n in RADA_ORDER if any(s["rada"] == n for s in sku_ctx)]
    order += [n for n in dict.fromkeys(s["rada"] for s in sku_ctx) if n not in order]
    groups = []
    for name in order:
        members = sorted((s for s in sku_ctx if s["rada"] == name),
                         key=lambda s: (STATUS_ORDER.index(s["status"]), -(s["saving"] if s["saving"] is not None else -1e12)))
        groups.append({"name": name, "rows": members})

    # side panel: changes
    name_by_sku = {s.sku_id: s.name for s in skus}
    susp_pairs = {(s["sku"], o["shop"]) for s in sku_ctx for o in s["susp"]}
    change_ctx = []
    if changes and len(changes) > 1:
        for c in (dict(zip(changes[0], line)) for line in changes[1:]):
            if c.get("SKU") not in name_by_sku:
                continue
            kinds = c.get("Typ změny", "")
            icon, tone = next(((i, t) for k, i, t in CHANGE_ICONS if k in kinds), ("↕", "neutral"))
            now, before, dp = _change_czk(c, "Nyní", rate), _change_czk(c, "Předtím", rate), _to_num(c.get("Δ %"))
            is_susp = (c["SKU"], c.get("Shop")) in susp_pairs
            text = f"{kinds}: {fmt_kc(now)}"
            if before is not None:
                text += f" (dřív {fmt_kc(before)}" + (f", {fmt_pct(dp)}" if dp is not None else "") + ")"
            change_ctx.append({"sku": c["SKU"], "name": name_by_sku[c["SKU"]], "icon": icon, "tone": "susp" if is_susp else tone,
                               "text": text + (" — podezřelá shoda, ověřit" if is_susp else ""),
                               "shop": c.get("Shop", ""), "url": c.get("URL", ""), "new_min": "nové 30denní minimum" in kinds})

    # side panel: problems
    shop_problems = []
    for shop in shops:
        rs = shop_rows.get(shop.id, [])
        blocked = sum(r["status"] == "blocked" for r in rs)
        priced = sum(r["status"] in ("ok", "implausible") for r in rs)
        if not rs:
            shop_problems.append({"shop": shop.name, "text": "shop dnes neprošel (žádná data v běhu)"})
        elif blocked == len(rs):
            shop_problems.append({"shop": shop.name, "text": f"blokuje robota — {len(rs)} párů bez ceny"})
        elif not priced:
            shop_problems.append({"shop": shop.name, "text": f"žádná cena — {len(rs)}× nenalezeno / chyba, zkontrolovat scraper"})
        elif blocked:
            shop_problems.append({"shop": shop.name, "text": f"{blocked}× blokováno"})
    susp_list = [{"sku": s["sku"], "name": s["name"], "count": f"{len(s['susp'])}×",
                  "examples": " · ".join(f"{o['shop']} {fmt_kc(o['kc'])} „{o['r'].get('title', '')}“" for o in s["susp"])}
                 for s in sku_ctx if s["susp"]]
    n_susp = sum(len(s["susp"]) for s in sku_ctx)

    cheaper = [s for s in sku_ctx if s["status"] == "cheaper"]
    count = lambda st: sum(s["status"] == st for s in sku_ctx)  # noqa: E731
    run_date = max((r["date"] for r in rows), default="")
    return {
        "run_date": run_date, "run_ts": max((r.get("run_ts", "") for r in rows), default=""),
        "generated": generated or datetime.now().strftime("%Y-%m-%d %H:%M"),
        "rate": fmt_rate(rate), "rate_date": (fx or {}).get("date") or "záložní kurz", "threshold": fmt_n(threshold),
        "low_pct": fmt_n(low_pct), "shops": [s.name for s in shops], "groups": groups, "skus": sku_ctx,
        "kpi": {"cheaper": len(cheaper), "savings": fmt_kc(sum(s["saving"] for s in cheaper)), "near": count("near"),
                "over": count("over"), "nosup": count("nosup"), "changes": len(change_ctx),
                "changes_note": f"{sum(c['new_min'] for c in change_ctx)}× nové 30d minimum",
                "problems": n_susp + len(shop_problems),
                "problems_note": f"{n_susp} podezřelých shod · {len(shop_problems)} shopů s problémem"},
        "changes": change_ctx, "shop_problems": shop_problems, "susp_list": susp_list,
        "n_pairs": len(rows), "n_supplier": sum(s["sup"] is not None for s in sku_ctx),
    }


def _change_czk(c: dict, which: str, rate):
    """Price from a Změny row; a changes.csv written before the CZK switch only has the € column."""
    v = _to_num(c.get(f"{which} Kč bez DPH"))
    return v if v is not None else to_czk(_to_num(c.get(f"{which} € bez DPH")), rate)


def _to_num(v):
    try:
        return float(v) if v not in ("", None) else None
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- rendering
CSS = """
:root{--bg:#f4f5f7;--surface:#fff;--alt:#f8f9fb;--hover:#f3f5f8;--border:#e2e4e8;--divider:#eceef1;--ctl:#dcdfe4;
--track:#eef0f3;--ink:#1a1c1e;--ink2:#4a4f56;--muted:#6b7076;--disabled:#9aa0a8;--faint:#c3c7cd;--accent:#1f4fa3;
--cheaper-bg:oklch(0.95 0.05 150);--cheaper-fg:oklch(0.42 0.13 150);--near-bg:oklch(0.96 0.06 85);--near-fg:oklch(0.48 0.12 70);
--over-bg:oklch(0.95 0.04 22);--over-fg:oklch(0.5 0.17 22);--susp-bg:oklch(0.95 0.04 300);--susp-fg:oklch(0.45 0.15 300);
--neutral-bg:#eef0f2;--neutral-fg:#5b6066}
html,body{margin:0;padding:0;background:var(--bg);color:var(--ink);font-family:'IBM Plex Sans',system-ui,sans-serif;
-webkit-font-smoothing:antialiased}
*{box-sizing:border-box}a{color:var(--accent)}a:hover{color:#163a7a}button{font:inherit;cursor:pointer}input{font:inherit}
[hidden]{display:none!important}
.page{min-height:100vh;display:flex;flex-direction:column;font-size:13px;line-height:1.4}
.num{font-variant-numeric:tabular-nums}.r{text-align:right}.mono{font-family:'IBM Plex Mono',monospace}
.ell{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.muted{color:var(--muted)}
header{position:sticky;top:0;z-index:20;background:#fff;border-bottom:1px solid var(--border);padding:10px 20px;display:flex;
flex-wrap:wrap;align-items:center;gap:10px 20px}
.brand{display:flex;align-items:baseline;gap:10px}.brand b{font-weight:600;font-size:16px;letter-spacing:-.01em}
.meta{display:flex;flex-wrap:wrap;gap:6px 18px;color:var(--muted);font-variant-numeric:tabular-nums}
.meta strong{color:var(--ink);font-weight:500}
.tabs{margin-left:auto;display:flex;background:var(--track);border-radius:8px;padding:3px;gap:2px}
.tabs button{border:0;border-radius:6px;padding:6px 14px;font-weight:500;background:transparent;color:var(--muted)}
.tabs button.on{background:#fff;color:var(--ink);box-shadow:0 1px 2px rgba(0,0,0,.08)}
.kpis{padding:16px 20px 0;display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}
.kpi{background:#fff;border:1px solid var(--border);border-left:4px solid var(--disabled);border-radius:8px;padding:12px 14px}
.kpi .l{color:var(--muted);font-size:12px}.kpi .v{font-size:26px;font-weight:600;line-height:1.1;margin-top:4px;font-variant-numeric:tabular-nums}
.kpi .v span{font-size:13px;font-weight:400;color:var(--muted)}.kpi .s{margin-top:4px;color:var(--muted)}
.kpi.cheaper{border-left-color:oklch(0.55 0.15 150)}.kpi.cheaper .s{color:var(--cheaper-fg);font-weight:500}
.kpi.near{border-left-color:oklch(0.75 0.14 80)}.kpi.over{border-left-color:oklch(0.6 0.17 22)}
.kpi.changes{border-left-color:var(--accent)}.kpi.problems{border-left-color:oklch(0.5 0.15 300)}
.filters{padding:14px 20px 0;display:flex;flex-wrap:wrap;gap:8px;align-items:center}.chips{display:flex;flex-wrap:wrap;gap:4px}
.chip{border:1px solid var(--ctl);background:#fff;color:var(--ink);border-radius:999px;padding:5px 11px;font-size:12px;font-weight:500}
.chip.on{border-color:var(--ink);background:var(--ink);color:#fff}.chip i{opacity:.6;font-weight:400;font-style:normal}
.sep{width:1px;height:22px;background:var(--ctl)}
#q{margin-left:auto;border:1px solid var(--ctl);border-radius:8px;padding:6px 10px;min-width:200px;background:#fff;outline:none}
.body{padding:14px 20px 32px;display:flex;flex-wrap:wrap;gap:16px;align-items:flex-start}
main{flex:1 1 680px;min-width:0}aside{flex:0 1 300px;min-width:260px;display:flex;flex-direction:column;gap:12px}
.card{background:#fff;border:1px solid var(--border);border-radius:10px;overflow-x:auto}
.buy-g,.det-g,.mx-g{display:grid;align-items:center}
.buy-g{grid-template-columns:minmax(230px,2fr) 150px minmax(170px,1.3fr) 110px 150px 120px 96px;gap:0 12px}
.th{padding:8px 14px;border-bottom:1px solid var(--border);color:var(--muted);font-size:11px;text-transform:uppercase;
letter-spacing:.04em;font-weight:500}
.grp-h{padding:10px 14px 6px;background:var(--alt);border-bottom:1px solid var(--divider);display:flex;align-items:baseline;gap:8px}
.grp-h b{font-weight:600}.grp-h span{color:var(--muted);font-size:12px}
.row{padding:9px 14px;border-bottom:1px solid var(--divider);cursor:pointer}.row:hover,.sku.open>.row{background:var(--hover)}
.sku.flash>.row{outline:2px solid var(--accent);outline-offset:-2px}
.nm{font-weight:500;display:flex;gap:6px;align-items:baseline;min-width:0}.nm .chev{color:var(--disabled);font-size:10px;width:10px;flex:none}
.id{color:var(--muted);font-size:11px;padding-left:16px}
.badge{display:inline-block;border-radius:6px;padding:3px 8px;font-size:12px;font-weight:500;white-space:nowrap}
.price{font-weight:600;font-size:14px}.shop{color:var(--muted);font-size:12px}
.t-cheaper{background:var(--cheaper-bg);color:var(--cheaper-fg)}.t-near{background:var(--near-bg);color:var(--near-fg)}
.t-over{background:var(--over-bg);color:var(--over-fg)}.t-susp{background:var(--susp-bg);color:var(--susp-fg)}
.t-nosup,.t-none,.t-neutral{background:var(--neutral-bg);color:var(--neutral-fg)}.t-accent{background:#e8eefb;color:var(--accent)}
.c-cheaper{color:var(--cheaper-fg)}.c-near{color:var(--near-fg)}.c-over{color:var(--over-fg)}.c-susp{color:var(--susp-fg)}
.c-muted,.c-nosup{color:var(--muted)}
.tags{display:flex;flex-wrap:wrap;gap:4px}.tag{font-size:11px;border-radius:4px;padding:2px 6px;white-space:nowrap}
.go{display:inline-block;text-decoration:none;border:1px solid #c9d6ee;background:#f2f6fd;color:var(--accent);border-radius:6px;
padding:4px 9px;font-size:12px;font-weight:500;white-space:nowrap}.go:hover{background:#e3ecfa}
.det{background:var(--alt);border-bottom:1px solid var(--border);padding:12px 14px 14px 40px}
.det-h{display:flex;flex-wrap:wrap;gap:6px 18px;align-items:baseline;margin-bottom:8px}.det-h b{font-weight:600}
.det-h .spark{margin-left:auto;display:flex;align-items:center;gap:6px;color:var(--muted);font-size:11px}
.det-g{grid-template-columns:150px minmax(200px,2fr) 110px 110px 60px 110px 110px minmax(120px,1fr) 80px;gap:0 10px}
.det-th{padding:4px 10px;color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.04em}
.off{padding:6px 10px;border-radius:6px;margin-bottom:2px;font-variant-numeric:tabular-nums;font-size:12px}
.off.none{color:var(--disabled)}.off.best{font-weight:400}.off.best .b{font-weight:600}.off a{font-weight:500;text-decoration:none}
.stav{display:flex;flex-wrap:wrap;gap:4px}.stav span:first-child{border-radius:4px;padding:1px 6px;white-space:nowrap}
.src{color:var(--muted);font-size:11px;margin-top:8px}
.mx-th{padding:8px 14px;border-bottom:1px solid var(--border);color:var(--muted);font-size:11px;font-weight:500;letter-spacing:.02em}
.mx-g{gap:0 4px}.mrow{padding:4px 14px;border-bottom:1px solid var(--divider);cursor:pointer}.mrow:hover,.sku.open>.mrow{background:var(--hover)}
.cell{text-align:right;padding:6px 8px;border-radius:5px;font-variant-numeric:tabular-nums;font-size:12px}
.cell.faint{color:var(--faint)}.cell.blocked{color:var(--disabled)}.cell.susp{background:var(--susp-bg);color:var(--susp-fg)}
.cell.nosup{background:var(--neutral-bg);color:var(--ink2)}.cell.ring{box-shadow:inset 0 0 0 2px var(--ink);font-weight:600}
.legend{display:flex;flex-wrap:wrap;gap:6px 14px;padding:10px 14px;border-top:1px solid var(--border);font-size:12px;color:var(--ink2)}
.legend i{display:inline-block;width:12px;height:12px;border-radius:3px;vertical-align:-2px;margin-right:4px}
.panel{background:#fff;border:1px solid var(--border);border-radius:10px;padding:12px 14px}
.panel h2{font-size:13px;font-weight:600;margin:0 0 8px;display:flex;justify-content:space-between}.panel h2 span{color:var(--muted);font-weight:400}
.ch{display:flex;gap:10px;padding:7px 0;border-top:1px solid var(--divider)}
.ch .ico{width:26px;height:26px;border-radius:6px;display:flex;align-items:center;justify-content:center;font-weight:600;font-size:12px;flex:none}
.ch .tx{font-size:12px;color:var(--ink2)}.ch .ac{font-size:12px;display:flex;gap:8px}.ch .ac a{text-decoration:none;font-weight:500}
.link{border:0;background:none;padding:0;color:var(--accent);font-weight:500;text-align:left}.link.ink{color:var(--ink)}
.pr{display:flex;gap:8px;padding:6px 0;border-top:1px solid var(--divider);font-size:12px}.pr>b{font-weight:600}
.pr strong{font-weight:500}
.how{font-size:12px;color:var(--ink2)}.how dl{display:grid;grid-template-columns:auto 1fr;gap:5px 8px;align-items:baseline;margin:0}
.how dt span{border-radius:5px;padding:1px 6px;font-weight:500;white-space:nowrap}.how dd{margin:0}
.empty{padding:24px 14px;color:var(--muted)}
footer{margin-top:auto;padding:12px 20px;border-top:1px solid var(--border);color:var(--muted);font-size:12px;display:flex;flex-wrap:wrap;gap:6px 18px}
@media print{header{position:static}.tabs,.filters,.go,.ch .ac{display:none!important}.card{overflow:visible}body{background:#fff}}
"""

JS = """
(function(){
var S={view:'buy',rada:'',sklad:false,sup:false,q:'',open:{}};
var $=function(s,r){return Array.prototype.slice.call((r||document).querySelectorAll(s))};
function kc(n){return Math.round(n).toString().replace(/\\B(?=(\\d{3})+(?!\\d))/g,' ')+' Kč'}
function readHash(){location.hash.slice(1).split('&').forEach(function(p){var kv=p.split('='),k=kv[0],v=decodeURIComponent(kv[1]||'');
 if(k==='view'&&(v==='buy'||v==='matrix'))S.view=v;if(k==='rada')S.rada=v;if(k==='q')S.q=v;if(k==='sklad')S.sklad=v==='1';
 if(k==='sup')S.sup=v==='1';if(k==='sku')v.split(',').forEach(function(x){if(x)S.open[x]=true})})}
function writeHash(){var p=[];if(S.view!=='buy')p.push('view='+S.view);if(S.rada)p.push('rada='+encodeURIComponent(S.rada));
 if(S.sklad)p.push('sklad=1');if(S.sup)p.push('sup=1');if(S.q)p.push('q='+encodeURIComponent(S.q));
 var o=Object.keys(S.open).filter(function(k){return S.open[k]});if(o.length)p.push('sku='+o.join(','));
 history.replaceState(null,'',p.length?'#'+p.join('&'):location.pathname+location.search)}
function apply(){
 $('[data-view]').forEach(function(b){b.classList.toggle('on',b.dataset.view===S.view)});
 $('.view').forEach(function(v){v.hidden=v.id!=='view-'+S.view});
 $('[data-rada-chip]').forEach(function(b){b.classList.toggle('on',b.dataset.radaChip===S.rada)});
 var ts=document.getElementById('f-sklad'),tc=document.getElementById('f-sup');
 ts.classList.toggle('on',S.sklad);ts.firstChild.nodeValue=(S.sklad?'☑':'☐')+' Jen skladem';
 tc.classList.toggle('on',S.sup);tc.firstChild.nodeValue=(S.sup?'☑':'☐')+' Jen s cenou dodavatele';
 var q=S.q.trim().toLowerCase(),inp=document.getElementById('q');if(inp.value!==S.q)inp.value=S.q;
 $('.sku').forEach(function(el){var d=el.dataset;
  el.hidden=!((!S.rada||d.rada===S.rada)&&(!S.sklad||d.sklad==='1')&&(!S.sup||d.sup==='1')&&(!q||d.q.indexOf(q)>=0));
  var on=!!S.open[d.sku];el.classList.toggle('open',on);var det=el.querySelector('.det');if(det)det.hidden=!on;
  var ch=el.querySelector('.chev');if(ch)ch.textContent=on?'▾':'▸'});
 $('.grp').forEach(function(g){var vis=$('.sku',g).filter(function(e){return !e.hidden});g.hidden=!vis.length;
  var sum=g.querySelector('.grp-h span');if(!sum)return;var ch=vis.filter(function(e){return e.dataset.status==='cheaper'});
  var tot=ch.reduce(function(s,e){return s+(+e.dataset.saving||0)},0);
  sum.textContent=vis.length+' SKU'+(ch.length?' · '+ch.length+' levněji, úspora '+kc(tot):' · nic levněji než dodavatel')});
 $('.view').forEach(function(v){var e=v.querySelector('.empty');if(e)e.hidden=$('.sku',v).some(function(x){return !x.hidden})});
 writeHash()}
function jump(sku){S.view='buy';S.rada='';S.sklad=false;S.sup=false;S.q=sku;S.open[sku]=true;apply();
 var el=document.querySelector('#view-buy .sku[data-sku="'+sku+'"]');if(!el)return;
 var top=el.getBoundingClientRect().top+window.pageYOffset-document.querySelector('header').offsetHeight-8;
 window.scrollTo({top:top,behavior:'smooth'});el.classList.add('flash');setTimeout(function(){el.classList.remove('flash')},1200)}
document.addEventListener('click',function(e){var t=e.target;
 if(t.closest('a'))return;
 var b=t.closest('[data-view]');if(b){S.view=b.dataset.view;return apply()}
 b=t.closest('[data-rada-chip]');if(b){S.rada=b.dataset.radaChip;return apply()}
 if(t.closest('#f-sklad')){S.sklad=!S.sklad;return apply()}
 if(t.closest('#f-sup')){S.sup=!S.sup;return apply()}
 b=t.closest('[data-jump]');if(b)return jump(b.dataset.jump);
 b=t.closest('.row,.mrow');if(b){var s=b.parentNode.dataset.sku;S.open[s]=!S.open[s];apply()}});
document.getElementById('q').addEventListener('input',function(e){S.q=e.target.value;apply()});
readHash();apply();
})();
"""


def _e(v) -> str:
    return escape(str(v if v is not None else ""), quote=True)


def _sparkline(series: list[tuple[str, int]]) -> str:
    if len(series) < 2:
        return ""
    vals = [v for _, v in series]
    lo, hi = min(vals), max(vals)
    w, h, pad = 120, 26, 3
    span = (hi - lo) or 1
    pts = " ".join(f"{pad + i * (w - 2 * pad) / (len(vals) - 1):.1f},{h - pad - (v - lo) / span * (h - 2 * pad):.1f}"
                   for i, v in enumerate(vals))
    title = f"Vývoj minima {series[0][0]} – {series[-1][0]}: {fmt_kc(lo)} až {fmt_kc(hi)}"
    return (f'<span class="spark" title="{_e(title)}">minimum za {len(vals)} dní'
            f'<svg width="{w}" height="{h}" viewBox="0 0 {w} {h}" role="img" aria-label="{_e(title)}">'
            f'<polyline points="{pts}" fill="none" stroke="#1f4fa3" stroke-width="1.5" stroke-linejoin="round"/></svg>'
            f'<span class="num">{_e(fmt_kc(lo) if lo == hi else fmt_kc(lo) + " – " + fmt_kc(hi))}</span></span>')


def _detail_html(s: dict, ctx: dict) -> str:
    out = [f'<div class="det" hidden><div class="det-h"><b>Všechny shopy · {_e(s["name"])}</b>'
           f'<span class="muted" style="font-size:12px">{_e(s["detail_note"])}</span>{_sparkline(s["series"])}</div>',
           '<div class="det-g det-th"><div>Shop</div><div>Název v shopu</div><div class="r">Cena v shopu</div>'
           '<div class="r">Kč bez DPH</div><div>DPH</div><div class="r">vs dodavatel</div><div>Dostupnost</div>'
           '<div>Stav · poznámka</div><div></div></div>']
    for o in s["detail"]:
        cls = "off det-g"
        if o["best"]:
            cls += f' best t-{s["status"]}'
        elif o["kind"] == "susp":
            cls += " t-susp"
        elif not o["kc"]:
            cls += " none"
        stav_tone = "susp" if o["kind"] == "susp" else "over" if o["kind"] == "blocked" else "neutral"
        pct_tone = o["kind"] if o["kind"] in ("cheaper", "near", "over", "susp") else "muted"
        link = f'<a href="{_e(o["url"])}" target="_blank" rel="noopener">Otevřít ↗</a>' if o["url"] else ""
        out.append(
            f'<div class="{cls}" style="color:var(--ink)"><div class="b">{_e(o["rank"])}{_e(o["shop"])}</div>'
            f'<div class="ell" title="{_e(o["name"])}">{_e(o["name"])}</div><div class="r">{_e(o["price_shop"])}</div>'
            f'<div class="r b">{_e(o["kc"])}</div><div class="muted">{_e(o["dph"])}</div>'
            f'<div class="r c-{pct_tone}" style="font-weight:500">{_e(o["pct"])}</div><div>{_e(o["avail"])}</div>'
            f'<div class="stav"><span class="t-{stav_tone}">{_e(o["stav"])}</span><span class="muted">{_e(o["note"])}</span></div>'
            f'<div class="r">{link}</div></div>' if o["kc"] else
            f'<div class="{cls}"><div>{_e(o["shop"])}</div><div></div><div></div><div></div><div>{_e(o["dph"])}</div><div></div>'
            f'<div></div><div class="stav"><span class="t-{stav_tone}">{_e(o["stav"])}</span></div><div></div></div>')
    out.append(f'<div class="src">Zdroj: data/latest.json → SKU {_e(s["sku"])} (list Detail v Google Sheetu), běh {_e(ctx["run_date"])}, '
               f'přepočet kurzem {_e(ctx["rate"])} Kč/€. Kč bez DPH = cena v shopu bez DPH země shopu, převedená na Kč.</div></div>')
    return "".join(out)


def _sku_attrs(s: dict) -> str:
    return (f'class="sku" data-sku="{_e(s["sku"])}" data-rada="{_e(s["rada"])}" data-sklad="{int(s["in_stock"])}" '
            f'data-sup="{int(s["sup"] is not None)}" data-status="{s["status"]}" '
            f'data-saving="{round(s["saving"]) if s["saving"] is not None else ""}" '
            f'data-q="{_e((s["name"] + " " + s["sku"]).lower())}"')


def render(ctx: dict) -> str:
    k = ctx["kpi"]
    p = ctx["threshold"]
    n_shops = len(ctx["shops"])
    h: list[str] = []
    h.append(f'<!DOCTYPE html><html lang="cs"><head><meta charset="utf-8">'
             f'<meta name="viewport" content="width=device-width, initial-scale=1"><meta name="robots" content="noindex">'
             f'<title>Pricebot · {_e(ctx["run_date"])}</title>'
             f'<link rel="preconnect" href="https://fonts.googleapis.com">'
             f'<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;500;600&amp;family=IBM+Plex+Mono:wght@400;500&amp;display=swap" rel="stylesheet">'
             f'<style>{CSS}</style></head><body><div class="page">')
    h.append(f'<header><div class="brand"><b>Pricebot</b><span class="muted">Velomania · interní, důvěrné</span></div>'
             f'<div class="meta"><span>Běh <strong>{_e(ctx["run_date"])}</strong></span>'
             f'<span>Kurz ECB <strong>{_e(ctx["rate"])} Kč/€</strong> k {_e(ctx["rate_date"])}</span>'
             f'<span>Práh <strong>+{_e(p)} %</strong></span></div>'
             f'<div class="tabs"><button data-view="buy">Kde koupit</button><button data-view="matrix">Matice shopů</button></div></header>')

    def kpi(cls, label, value, sub, unit="SKU"):
        u = f' <span>{unit}</span>' if unit else ""
        return f'<div class="kpi {cls}"><div class="l">{_e(label)}</div><div class="v">{_e(value)}{u}</div><div class="s">{_e(sub)}</div></div>'

    h.append('<div class="kpis">' + kpi("cheaper", "▼ Levněji než dodavatel", k["cheaper"], f'možná úspora {k["savings"]}')
             + kpi("near", f"≈ Do +{p} % nad dodavatele", k["near"], "objednat u dodavatele")
             + kpi("over", "▲ Vše nad prahem", k["over"], f"trh dražší o víc než {p} %")
             + kpi("nosup", "– Bez ceny dodavatele", k["nosup"], "jen tržní minimum")
             + kpi("changes", "↕ Změny od včera", k["changes"], k["changes_note"], "")
             + kpi("problems", "? Problémy", k["problems"], k["problems_note"], "") + "</div>")

    chips = [f'<button class="chip" data-rada-chip="">Vše <i>{len(ctx["skus"])}</i></button>']
    chips += [f'<button class="chip" data-rada-chip="{_e(g["name"])}">{_e(g["name"])} <i>{len(g["rows"])}</i></button>' for g in ctx["groups"]]
    h.append(f'<div class="filters"><div class="chips">{"".join(chips)}</div><span class="sep"></span>'
             f'<button class="chip" id="f-sklad">☐ Jen skladem</button><button class="chip" id="f-sup">☐ Jen s cenou dodavatele</button>'
             f'<input id="q" placeholder="Hledat název nebo ID…" autocomplete="off"></div>')

    h.append('<div class="body"><main>')
    # ---- Kde koupit
    h.append('<div class="view card" id="view-buy"><div style="min-width:900px"><div class="buy-g th"><div>Díl</div><div>Stav</div>'
             '<div>Nejnižší cena · shop</div><div class="r">Dodavatel</div><div class="r">Rozdíl</div><div>Dostupnost · důvěra</div><div></div></div>')
    for g in ctx["groups"]:
        h.append(f'<section class="grp"><div class="grp-h"><b>{_e(g["name"])}</b><span></span></div>')
        for s in g["rows"]:
            tags = "".join(f'<span class="tag t-{t["tone"]}" title="{_e(t["title"])}">{_e(t["label"])}</span>' for t in s["tags"])
            go = f'<a class="go" href="{_e(s["url"])}" target="_blank" rel="noopener">Do shopu ↗</a>' if s["url"] else ""
            h.append(f'<div {_sku_attrs(s)}><div class="row buy-g"><div style="min-width:0"><div class="nm"><span class="chev">▸</span>'
                     f'<span class="ell" title="{_e(s["name"])}">{_e(s["name"])}</span></div>'
                     f'<div class="id mono">{_e(s["sku"])} · {_e(s["kategorie"])}</div></div>'
                     f'<div><span class="badge t-{s["status"]}">{_e(s["badge"])}</span>'
                     + (f'<div class="c-{s["status"]}" style="font-size:11px;margin-top:2px">{_e(s["badge_sub"])}</div>' if s["badge_sub"] else "")
                     + f'</div>'
                     f'<div style="min-width:0"><div class="price num">{_e(s["min_text"])}</div><div class="shop ell">{_e(s["min_shop"])}</div></div>'
                     f'<div class="r num" style="color:var(--ink2)">{_e(s["sup_text"])}</div>'
                     f'<div class="r c-{s["diff_tone"]}"><div class="num" style="font-weight:600">{_e(s["diff_pct"])}</div>'
                     f'<div style="font-size:12px">{_e(s["diff_kc"])}</div></div><div class="tags">{tags}</div><div class="r">{go}</div></div>'
                     f'{_detail_html(s, ctx)}</div>')
        h.append("</section>")
    h.append('<div class="empty" hidden>Filtru neodpovídá žádný díl.</div></div></div>')

    # ---- Matice shopů
    cols = f"grid-template-columns:minmax(240px,1.6fr) 100px repeat({n_shops},minmax(92px,1fr))"
    h.append(f'<div class="view card" id="view-matrix" hidden><div style="min-width:1000px"><div class="mx-g mx-th" style="{cols}">'
             f'<div style="text-transform:uppercase">Díl</div><div class="r" style="text-transform:uppercase">Dodavatel</div>'
             + "".join(f'<div class="r ell" title="{_e(n)}">{_e(n)}</div>' for n in ctx["shops"]) + "</div>")
    for g in ctx["groups"]:
        h.append(f'<section class="grp"><div class="grp-h" style="padding:8px 14px 4px"><b>{_e(g["name"])}</b></div>')
        for s in g["rows"]:
            cells = "".join(
                f'<div class="cell {c["cls"]}{" ring" if c["ring"] else ""}" title="{_e(c["title"])}"'
                + (f' style="background:{c["bg"]};color:{c["fg"]}"' if c["bg"] else "") + f'>{_e(c["text"])}</div>' for c in s["cells"])
            h.append(f'<div {_sku_attrs(s)}><div class="mrow mx-g" style="{cols}"><div style="min-width:0">'
                     f'<div class="ell" style="font-weight:500" title="{_e(s["name"])}">{_e(s["name"])}</div>'
                     f'<div class="mono muted" style="font-size:11px">{_e(s["sku"])}</div></div>'
                     f'<div class="r num" style="color:var(--ink2)">{_e(s["sup_short"])}</div>{cells}</div>{_detail_html(s, ctx)}</div>')
        h.append("</section>")
    h.append(f'<div class="empty" hidden>Filtru neodpovídá žádný díl.</div></div><div class="legend">'
             f'<span><i style="background:oklch(0.9 0.1 150)"></i>levněji než dodavatel (sytější = víc)</span>'
             f'<span><i style="background:oklch(0.95 0.07 85)"></i>do +{_e(p)} %</span>'
             f'<span><i style="background:oklch(0.94 0.045 22)"></i>nad prahem</span>'
             f'<span><i style="background:var(--susp-bg)"></i>? podezřelá shoda</span>'
             f'<span><i style="box-shadow:inset 0 0 0 2px #1a1c1e"></i>minimum v řádku</span>'
             f'<span class="muted">čísla v Kč bez DPH &nbsp; · nenalezeno &nbsp; ⊘ blokováno &nbsp; ∅ shop dnes neprošel &nbsp; šedá = bez ceny dodavatele</span>'
             f'</div></div></main>')

    # ---- side panel
    h.append(f'<aside><section class="panel"><h2>Změny od včera<span>{k["changes"]}</span></h2>')
    for c in ctx["changes"]:
        shop = f'<a href="{_e(c["url"])}" target="_blank" rel="noopener">{_e(c["shop"])} ↗</a>' if c["url"] else _e(c["shop"])
        h.append(f'<div class="ch"><div class="ico t-{c["tone"]}">{_e(c["icon"])}</div><div style="min-width:0;flex:1">'
                 f'<div class="ell" style="font-weight:500" title="{_e(c["name"])}">{_e(c["name"])}</div><div class="tx">{_e(c["text"])}</div>'
                 f'<div class="ac"><button class="link" data-jump="{_e(c["sku"])}">Zobrazit řádek</button>{shop}</div></div></div>')
    if not ctx["changes"]:
        h.append('<div class="muted">Žádné změny ≥ 3 %.</div>')
    h.append(f'</section><section class="panel"><h2>Problémy k ověření<span>{k["problems"]}</span></h2>')
    for b in ctx["shop_problems"]:
        h.append(f'<div class="pr"><b class="c-over">⊘</b><span><strong>{_e(b["shop"])}</strong> — {_e(b["text"])}</span></div>')
    for s in ctx["susp_list"]:
        h.append(f'<div class="pr"><b class="c-susp">?</b><div style="min-width:0"><div><button class="link ink" data-jump="{_e(s["sku"])}">'
                 f'{_e(s["name"])}</button> <span class="muted">{_e(s["count"])}</span></div>'
                 f'<div class="muted ell" title="{_e(s["examples"])}">{_e(s["examples"])}</div></div></div>')
    if not ctx["shop_problems"] and not ctx["susp_list"]:
        h.append('<div class="muted">Nic k ověření.</div>')
    h.append(f'</section><section class="panel how"><h2 style="color:var(--ink)">Jak číst</h2><dl>'
             f'<dt><span class="t-cheaper">▼ levněji</span></dt><dd>trh je pod nákupní cenou dodavatele — kupte v shopu</dd>'
             f'<dt><span class="t-near">≈ do +{_e(p)} %</span></dt><dd>trh mírně dražší — objednejte u dodavatele</dd>'
             f'<dt><span class="t-over">▲ nad prahem</span></dt><dd>všechny nabídky dražší o víc než {_e(p)} %</dd>'
             f'<dt><span class="t-neutral">– bez ceny</span></dt><dd>dodavatel nemá ceníkovou položku</dd>'
             f'<dt><span class="t-susp">? ověřit</span></dt><dd>cena o víc než {_e(ctx["low_pct"])} % pod referencí (jiný díl) — z minima vyřazena</dd></dl>'
             f'<div style="margin-top:8px">Ceny v Kč bez DPH, celé koruny. Rozdíl = nejnižší platná cena − cena dodavatele.</div></section></aside></div>')

    h.append(f'<footer><span>Zdroj dat: <code>data/latest.json</code> + <code>dodavatel.csv</code> ({ctx["n_supplier"]} SKU s cenou), '
             f'běh {_e(ctx["run_ts"] or ctx["run_date"])}</span><span>Kurz CZK/EUR {_e(ctx["rate"])} (ECB, {_e(ctx["rate_date"])})</span>'
             f'<span>{len(ctx["skus"])} SKU × {n_shops} shopů · {ctx["n_pairs"]} párů</span><span>Vygenerováno {_e(ctx["generated"])}</span></footer>')
    h.append(f"</div><script>{JS}</script></body></html>")
    return "".join(h)


def write(ctx: dict, path: Path = DATA / "dashboard.html") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render(ctx), encoding="utf-8")
    return path


def read_changes_csv(path: Path = DATA / "changes.csv") -> list[list]:
    """changes.csv as written by the last run (the GitHub Action commits it) -> table for build_context."""
    import csv

    if not path.exists():
        return []
    with path.open(encoding="utf-8", newline="") as fh:
        return [row for row in csv.reader(fh)]

