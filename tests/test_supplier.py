"""Supplier (Paul Lange) purchase prices: loader + threshold filter in the report."""
from pricebot import report
from pricebot.config import Shop, load_settings, load_skus, load_supplier

FX = {"rate": 25.0, "date": "2026-09-18"}
SHOPS = [Shop(id=i, name=f"{i}.test", country="DE", currency="EUR", vat=0.19, search_url="https://x/{q}")
         for i in ("a", "b", "c")]


def offer(sku_id, shop_id, czk_net):
    eur_net = czk_net / FX["rate"]
    return {"date": "2026-09-19", "run_ts": "ts", "sku_id": sku_id, "shop_id": shop_id, "status": "ok",
            "title": f"{sku_id} @ {shop_id}", "url": f"https://{shop_id}.test/{sku_id}", "price_local": round(eur_net * 1.19, 2),
            "currency": "EUR", "price_eur": round(eur_net * 1.19, 2), "price_eur_net": eur_net,
            "price_czk_net": czk_net, "vat": 0.19, "availability": "in_stock", "source": "jsonld", "flag": ""}


def build(rows, supplier, sku_ids, pct=10):
    skus = [s for s in load_skus() if s.sku_id in sku_ids]
    settings = dict(load_settings(), supplier_threshold_pct=pct)
    return report.build(rows, skus, SHOPS, [], {}, settings, FX, supplier)


def as_dicts(table):
    return [dict(zip(table[0], line)) for line in table[1:]]


def test_load_supplier_decimal_comma_dot_bom_and_blanks(tmp_path):
    path = tmp_path / "dodavatel.csv"
    path.write_text("\ufeffsku_id,nazev,cena_czk_bez_dph,poznamka\n"
                    "A,dot,4130.00,Paul Lange A\n"
                    'B,comma,"2538,50",\n'
                    'C,thousands,"4 130,00 Kč",\n'
                    "D,empty,,na objednávku\n"
                    "E,text,na dotaz,\n"
                    "F,zero,0,\n"
                    "#G,comment,100,\n", encoding="utf-8")
    got = load_supplier(path)
    assert got == {"A": {"net_czk": 4130.0, "note": "Paul Lange A"},          # BOM did not eat the sku_id column
                   "B": {"net_czk": 2538.5, "note": ""},
                   "C": {"net_czk": 4130.0, "note": ""}}


def test_load_supplier_missing_file_is_empty(tmp_path):
    assert load_supplier(tmp_path / "neni.csv") == {}


def test_threshold_below_exact_and_above():
    sku = "SH-ULT-RD"
    rows = [offer(sku, "a", 900), offer(sku, "b", 1100), offer(sku, "c", 1101)]   # supplier 1000, limit 1100
    t = build(rows, {sku: {"net_czk": 1000.0, "note": ""}}, [sku])

    m = dict(zip(t["matrix"][0], t["matrix"][1]))
    assert (m["a.test"], m["b.test"], m["c.test"]) == (900, 1100, "")             # exactly on the limit passes
    assert m["Min Kč bez DPH"] == 900 and m["Dodavatel CZK bez DPH"] == 1000 and m["Δ % vs dodavatel"] == -10.0

    mn = as_dicts(t["minimum"])[0]
    assert (mn["Min Kč bez DPH"], mn["Δ % vs dodavatel"], mn["Úspora CZK"], mn["Shop"]) == (900, -10.0, 100, "a.test")
    assert "bez ceny dodavatele" not in mn["Poznámka"]

    over = as_dicts(t["over"])
    assert [(o["Shop"], o["Kč bez DPH"], o["Δ % vs dodavatel"], o["Úspora CZK"]) for o in over] == [("c.test", 1101, 10.1, -101)]
    assert t["over"][0][4:] == t["minimum"][0][4:]                                 # same columns as Minimum


def test_threshold_limit_is_not_lost_to_float_noise():
    sku = "SH-ULT-RD"
    t = build([offer(sku, "a", 4543)], {sku: {"net_czk": 4130.0, "note": ""}}, [sku])   # 4130 * 1.1 = 4543 exactly
    assert len(t["over"]) == 1 and as_dicts(t["minimum"])[0]["Min Kč bez DPH"] == 4543


def test_all_offers_above_threshold_go_to_over_sorted_by_excess():
    sku = "SH-ULT-RD"
    rows = [offer(sku, "a", 1500), offer(sku, "b", 1200), offer(sku, "c", 1350)]
    t = build(rows, {sku: {"net_czk": 1000.0, "note": ""}}, [sku])

    assert [o["Δ % vs dodavatel"] for o in as_dicts(t["over"])] == [20.0, 35.0, 50.0]   # smallest excess first
    mn = as_dicts(t["minimum"])[0]
    assert mn["Min Kč bez DPH"] == "" and mn["Dodavatel CZK bez DPH"] == 1000
    assert "nad prahem" in mn["Poznámka"] and "b.test" in mn["Poznámka"] and "+20.0 %" in mn["Poznámka"]
    assert len(t["changes"]) == 1                                                  # nothing in Změny
    assert t["min_rows"][0]["shop_id"] == "b"                                      # history keeps the market minimum


def test_sku_without_supplier_price_is_shown_with_note():
    sku = "SH-ULT-RD"
    t = build([offer(sku, "a", 900), offer(sku, "b", 99999)], {}, [sku])
    m = dict(zip(t["matrix"][0], t["matrix"][1]))
    assert (m["a.test"], m["b.test"], m["Dodavatel CZK bez DPH"], m["Δ % vs dodavatel"]) == (900, 99999, "", "")
    mn = as_dicts(t["minimum"])[0]
    assert mn["Min Kč bez DPH"] == 900 and mn["Dodavatel CZK bez DPH"] == "" and mn["Úspora CZK"] == ""
    assert "bez ceny dodavatele" in mn["Poznámka"]
    assert len(t["over"]) == 1


def test_matrix_sorted_by_saving_desc():
    ids = ["SH-ULT-RD", "SH-ULT-CS", "SH-ULT-FD", "SH-105-RD"]
    rows = [offer("SH-ULT-RD", "a", 950), offer("SH-ULT-CS", "a", 500), offer("SH-ULT-FD", "a", 700),
            offer("SH-105-RD", "a", 5000)]
    supplier = {"SH-ULT-RD": {"net_czk": 1000.0, "note": ""},      # saving 50
                "SH-ULT-CS": {"net_czk": 900.0, "note": ""},       # saving 400
                "SH-105-RD": {"net_czk": 1000.0, "note": ""}}      # above threshold -> no saving; FD has no supplier price
    t = build(rows, supplier, ids)
    assert [line[0] for line in t["matrix"][1:3]] == ["SH-ULT-CS", "SH-ULT-RD"]
    assert {line[0] for line in t["matrix"][3:]} == {"SH-ULT-FD", "SH-105-RD"}


def test_write_csvs_creates_nad_prahem(tmp_path):
    sku = "SH-ULT-RD"
    t = build([offer(sku, "a", 900), offer(sku, "b", 2000)], {sku: {"net_czk": 1000.0, "note": ""}}, [sku])
    report.write_csvs(t, tmp_path)
    text = (tmp_path / "nad-prahem.csv").read_text(encoding="utf-8")
    assert "b.test" in text and ",2000," in text and "2000.0" not in text          # whole crowns


def test_rows_of_a_switched_off_shop_are_ignored_not_fatal():
    """latest.json still holds rows of a shop disabled afterwards (rose/mantel): no KeyError, no column, no vote."""
    rows = [offer("SH-ULT-RD", "a", 900), offer("SH-ULT-RD", "gone", 2000)]        # 'gone' is not among SHOPS
    supplier = {"SH-ULT-RD": {"net_czk": 1000.0, "note": ""}}
    t = build(rows, supplier, ["SH-ULT-RD"])                                        # would raise KeyError before
    assert len(t["over"]) == 1 and as_dicts(t["minimum"])[0]["Shop"] == "a.test"       # header only: 2000 Kč of "gone" is not listed
    assert [r["shop_id"] for r in t["min_rows"]] == ["a"]


def test_export_and_dashboard_commands_use_only_enabled_shops(tmp_path, monkeypatch):
    import pricebot.__main__ as cli
    from pricebot import store

    data = tmp_path / "data"
    monkeypatch.setattr(store, "LATEST", data / "latest.json")
    monkeypatch.setattr(store, "HISTORY_MIN", data / "history_min.csv")
    monkeypatch.setattr(cli, "DATA", data)
    monkeypatch.setattr(cli, "load_supplier", lambda path=None: {"SH-ULT-RD": {"net_czk": 1000.0, "note": ""}})
    monkeypatch.setattr(cli, "load_shops", lambda: SHOPS)
    rows = [offer("SH-ULT-RD", "a", 900), offer("SH-ULT-RD", "gone", 300), offer("SR-OLD-PART", "a", 5)]
    store.save_latest(rows, "2026-09-19T04:10:00Z", FX)
    assert cli.main(["export"]) == 0 and cli.main(["dashboard"]) == 0
    html = (data / "dashboard.html").read_text(encoding="utf-8")
    assert "900 Kč" in html and "300 Kč" not in html
