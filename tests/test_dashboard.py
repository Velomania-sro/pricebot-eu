"""Static HTML dashboard: business logic from the design handoff, cs-CZ formatting, safe rendering."""
import re

import pricebot.__main__ as cli
from pricebot import config, dashboard, report, store
from pricebot.dashboard import fmt_kc, fmt_pct, fmt_shop_price, heat
from tests.test_supplier import FX, SHOPS, offer

SETTINGS = dict(config.load_settings(), supplier_threshold_pct=10, implausible_below_pct=40)
SKUS = {s.sku_id: s for s in config.load_skus()}


def ctx_for(rows, supplier, sku_ids, changes=None, history=None):
    report.flag_implausible(rows, supplier, SETTINGS, FX["rate"])
    skus = [SKUS[i] for i in sku_ids]
    return dashboard.build_context(rows, skus, SHOPS, SETTINGS, FX, supplier, changes, history, generated="2026-09-20 06:10")


def by_sku(ctx):
    return {s["sku"]: s for s in ctx["skus"]}


def test_formatting_cs():
    assert fmt_kc(16337) == "16 337 Kč" and fmt_kc(489) == "489 Kč" and fmt_kc(None) == "—"
    assert fmt_pct(-28.44) == "−28,4 %" and fmt_pct(12.35) == "+12,3 %" and fmt_pct(0.04) == "±0,0 %" and fmt_pct(None) == ""
    assert fmt_shop_price(749.99, "EUR") == "749,99 €" and fmt_shop_price(1299.0, "EUR") == "1 299,00 €"
    assert fmt_shop_price(5489.0, "CZK") == "5 489 Kč" and fmt_shop_price(156.0, "DKK") == "156,00 DKK"


def test_heat_scale():
    assert heat(0 - 1e-9, 10)[0].startswith("oklch(0.970") and heat(-40, 10)[0] == heat(-80, 10)[0] == "oklch(0.800 0.140 150)"
    assert heat(-30, 10)[1] == "#0f2a1a" and heat(-10, 10)[1] == "#1a1c1e"
    assert heat(10, 10)[0] == "oklch(0.95 0.07 85)" and heat(10.1, 10)[0] == "oklch(0.94 0.045 22)"


def test_row_states_and_kpis():
    supplier = {"SH-ULT-RD": {"net_czk": 1000.0}, "SH-ULT-FD": {"net_czk": 1000.0}, "SH-ULT-CS": {"net_czk": 1000.0},
                "SH-ULT-FC": {"net_czk": 1000.0}}
    rows = [offer("SH-ULT-RD", "a", 800), offer("SH-ULT-RD", "b", 900),          # cheaper, saving 200
            offer("SH-ULT-FD", "a", 1100),                                         # exactly +10 % -> near
            offer("SH-ULT-CS", "a", 1101), offer("SH-ULT-CS", "b", 1500),          # over, nearest +10.1 %
            offer("SH-ULT-FC", "a", 123),                                          # only an implausible offer -> susp
            offer("SH-ULT-CN", "a", 500)]                                          # no supplier price
    ids = ["SH-ULT-RD", "SH-ULT-FD", "SH-ULT-CS", "SH-ULT-FC", "SH-ULT-CN", "SH-ULT-STBR"]
    ctx = ctx_for(rows, supplier, ids)
    s = by_sku(ctx)
    assert {k: v["status"] for k, v in s.items()} == {"SH-ULT-RD": "cheaper", "SH-ULT-FD": "near", "SH-ULT-CS": "over",
                                                      "SH-ULT-FC": "susp", "SH-ULT-CN": "nosup", "SH-ULT-STBR": "none"}
    rd = s["SH-ULT-RD"]
    assert (rd["min_text"], rd["min_shop"], rd["diff_pct"], rd["diff_kc"]) == ("800 Kč", "a.test", "−20,0 %", "o 200 Kč levněji")
    assert s["SH-ULT-CS"]["badge"] == "▲ nad prahem" and s["SH-ULT-CS"]["badge_sub"] == "nejblíž +10,1 %"
    assert s["SH-ULT-FD"]["badge"] == "≈ do +10 %" and s["SH-ULT-FD"]["diff_kc"] == "o 100 Kč dráž"
    assert s["SH-ULT-FC"]["min_text"] == "jen podezřelé shody" and s["SH-ULT-FC"]["saving"] is None
    assert s["SH-ULT-CN"]["diff_kc"] == "bez srovnání" and s["SH-ULT-CN"]["sup_text"] == "—"

    k = ctx["kpi"]
    assert (k["cheaper"], k["savings"], k["near"], k["over"], k["nosup"]) == (1, "200 Kč", 1, 1, 1)
    assert k["problems"] == 1 + 1 and "1 podezřelých shod" in k["problems_note"]      # 1 suspicious offer + shop c without data
    assert [p["shop"] for p in ctx["shop_problems"]] == ["c.test"]

    order = [r["sku"] for g in ctx["groups"] for r in g["rows"]]
    assert order == ["SH-ULT-RD", "SH-ULT-FD", "SH-ULT-CS", "SH-ULT-FC", "SH-ULT-CN", "SH-ULT-STBR"]   # by state


def test_implausible_offer_never_becomes_the_minimum():
    rows = [offer("SH-ULT-RD", "a", 123), offer("SH-ULT-RD", "b", 950)]
    s = by_sku(ctx_for(rows, {"SH-ULT-RD": {"net_czk": 1000.0}}, ["SH-ULT-RD"]))["SH-ULT-RD"]
    assert s["min_text"] == "950 Kč" and s["status"] == "cheaper"
    assert [(d["shop"], d["rank"], d["kind"]) for d in s["detail"]] == [("b.test", "1. ", "cheaper"), ("a.test", "", "susp"),
                                                                        ("c.test", "", "nodata")]
    assert "jiný sortiment" in s["detail"][1]["note"] and s["detail"][1]["pct"].startswith("? −")
    cells = s["cells"]
    assert cells[0]["text"] == "? 123" and cells[0]["cls"] == "susp" and not cells[0]["ring"]
    assert cells[1]["text"] == "950" and cells[1]["ring"] and cells[2]["text"] == "∅"
    assert any(t["label"] == "? 1 podezř." for t in s["tags"])


def test_groups_follow_fixed_series_order_and_saving():
    supplier = {"SH-105-RD": {"net_czk": 1000.0}, "SH-DA-RD": {"net_czk": 1000.0}, "SH-DA-FD": {"net_czk": 1000.0}}
    rows = [offer("SH-DA-RD", "a", 950), offer("SH-DA-FD", "a", 700), offer("SH-105-RD", "a", 990), offer("SR-RED-SET", "a", 50000)]
    ctx = ctx_for(rows, supplier, ["SR-RED-SET", "SH-DA-RD", "SH-DA-FD", "SH-105-RD"])
    assert [g["name"] for g in ctx["groups"]] == ["105 Di2", "Dura-Ace Di2", "Red AXS"]
    assert [r["sku"] for r in ctx["groups"][1]["rows"]] == ["SH-DA-FD", "SH-DA-RD"]          # bigger saving first


def test_changes_feed_and_legacy_eur_changes_csv():
    rows = [offer("SH-ULT-RD", "a", 800)]
    changes = [["Datum", "SKU", "Název", "Typ změny", "Nyní Kč bez DPH", "Shop", "Předtím Kč bez DPH", "Shop předtím", "Δ %", "URL"],
               ["2026-09-19", "SH-ULT-RD", "x", "pokles ceny, nové 30denní minimum", 800, "a.test", 1000, "b.test", -20.0, "https://a.test/p"],
               ["2026-09-19", "SR-GONE", "x", "zdražení", 1, "a.test", 1, "a.test", 5.0, ""]]        # SKU no longer tracked
    ctx = ctx_for(rows, {}, ["SH-ULT-RD"], changes)
    assert len(ctx["changes"]) == 1 and ctx["kpi"]["changes"] == 1 and ctx["kpi"]["changes_note"] == "1× nové 30d minimum"
    c = ctx["changes"][0]
    assert c["icon"] == "↓30" and c["text"] == "pokles ceny, nové 30denní minimum: 800 Kč (dřív 1 000 Kč, −20,0 %)"

    legacy = [["Datum", "SKU", "Název", "Typ změny", "Nyní € bez DPH", "Shop", "Předtím € bez DPH", "Shop předtím", "Δ %", "URL"],
              ["2026-09-19", "SH-ULT-RD", "x", "zdražení", 40.0, "a.test", 32.0, "a.test", 25.0, ""]]
    c = ctx_for([offer("SH-ULT-RD", "a", 1000)], {}, ["SH-ULT-RD"], legacy)["changes"][0]
    assert c["icon"] == "▲" and c["text"] == "zdražení: 1 000 Kč (dřív 800 Kč, +25,0 %)"


def test_render_is_static_escaped_and_filterable():
    rows = [offer("SH-ULT-RD", "a", 800), offer("SH-ULT-RD", "b", 900)]
    rows[0]["title"] = '<script>alert(1)</script> "RD-R8150"'
    rows[0]["url"] = 'https://a.test/p?x="><img src=x onerror=alert(1)>'
    hist = {"SH-ULT-RD": [("2026-09-17", 40.0), ("2026-09-18", 36.0), ("2026-09-19", 32.0), ("2026-09-16", 4.0)]}
    html = dashboard.render(ctx_for(rows, {"SH-ULT-RD": {"net_czk": 1000.0}}, ["SH-ULT-RD"], history=hist))

    assert "<script>alert(1)" not in html and "&lt;script&gt;alert(1)" in html
    assert "<img src=x" not in html
    assert "{{" not in html and "support.js" not in html and "fetch(" not in html       # no prototype runtime
    assert html.count("<script>") == 1                                                    # only our inline script
    row = re.search(r'<div class="sku"[^>]*data-sku="SH-ULT-RD"[^>]*>', html).group(0)
    for attr in ('data-rada="Ultegra Di2"', 'data-sklad="1"', 'data-sup="1"', 'data-status="cheaper"', 'data-saving="200"'):
        assert attr in row
    assert 'data-q="' + SKUS["SH-ULT-RD"].name.lower() in row
    assert "800 Kč" in html and "−20,0 %" in html and "o 200 Kč levněji" in html
    assert 'target="_blank" rel="noopener"' in html
    assert "<polyline" in html and "minimum za 3 dní" in html                            # 100 Kč outlier dropped from the sparkline
    assert "24,339" not in html and "25,000 Kč/€" in html                                 # rate once in header + footer + sources
    assert "Vygenerováno 2026-09-20 06:10" in html


def test_dashboard_command_writes_ignored_file(tmp_path, monkeypatch, capsys):
    data = tmp_path / "data"
    monkeypatch.setattr(store, "LATEST", data / "latest.json")
    monkeypatch.setattr(store, "HISTORY_MIN", data / "history_min.csv")
    monkeypatch.setattr(cli, "DATA", data)
    monkeypatch.setattr(cli, "load_supplier", lambda path=None: {"SH-ULT-RD": {"net_czk": 1000.0, "note": ""}})
    monkeypatch.setattr(cli, "load_shops", lambda: SHOPS)
    rows = [offer("SH-ULT-RD", "a", 800)]
    for r in rows:
        r["shop_id"] = "a"
    store.save_latest(rows, "2026-09-19T04:10:00Z", FX)

    assert cli.main(["dashboard"]) == 0
    html = (data / "dashboard.html").read_text(encoding="utf-8")
    assert "Pricebot" in html and "800 Kč" in html and "1 000 Kč" in html
    assert "dashboard.html" in capsys.readouterr().out
