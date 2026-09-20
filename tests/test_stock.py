"""in_stock_only: only offers on the shelf count; availability is read from nested offers / visible text too."""
from pricebot import config, dashboard, report
from pricebot.parse import availability_from_text, parse_product_page
from tests.test_pipeline import PAD
from tests.test_supplier import FX, SHOPS, as_dicts, offer

SETTINGS = dict(config.load_settings(), in_stock_only=True, supplier_threshold_pct=10, implausible_below_pct=40)
SKU = "SH-ULT-RD"
SUP = {SKU: {"net_czk": 1000.0, "note": ""}}


def rows_with(avail_by_shop: dict[str, tuple[int, str]]):
    out = []
    for shop_id, (czk, availability) in avail_by_shop.items():
        r = offer(SKU, shop_id, czk)
        r["availability"] = availability
        out.append(r)
    return out


def build(rows, prev=(), settings=SETTINGS):
    skus = [s for s in config.load_skus() if s.sku_id == SKU]
    return report.build(rows, skus, SHOPS, list(prev), {}, settings, FX, SUP)


def test_minimum_ignores_offers_that_are_not_in_stock():
    t = build(rows_with({"a": (700, "out"), "b": (800, "unknown"), "c": (950, "in_stock")}))
    m = dict(zip(t["matrix"][0], t["matrix"][1]))
    assert (m["Min Kč bez DPH"], m["Nejlevnější shop"], m["a.test"], m["b.test"], m["c.test"]) == (950, "c.test", "", "", 950)
    assert as_dicts(t["minimum"])[0]["Úspora CZK"] == 50
    assert [r["shop_id"] for r in t["min_rows"]] == ["c"]                     # history keeps what could be bought
    assert len(t["detail"]) == 4                                               # Detail still lists all three offers


def test_limited_counts_backorder_and_preorder_do_not():
    t = build(rows_with({"a": (700, "backorder"), "b": (800, "preorder"), "c": (900, "limited")}))
    assert as_dicts(t["minimum"])[0]["Min Kč bez DPH"] == 900


def test_in_stock_at_the_shops_supplier_counts_and_is_labelled():
    rows = rows_with({"a": (700, "out"), "b": (800, "supplier"), "c": (950, "in_stock")})
    t = build(rows)
    mn = as_dicts(t["minimum"])[0]
    assert (mn["Min Kč bez DPH"], mn["Shop"], mn["Dostupnost"]) == (800, "b.test", "skladem u dodavatele")
    skus = [s for s in config.load_skus() if s.sku_id == SKU]
    s = dashboard.build_context(rows, skus, SHOPS, SETTINGS, FX, SUP)["skus"][0]
    assert s["in_stock"] and s["min_shop"] == "b.test"
    assert s["tags"][0]["label"] == "◐ u dodavatele" and "doručení bývá o pár dní delší" in s["tags"][0]["title"]
    assert [d["kind"] for d in s["detail"][:2]] == ["cheaper", "cheaper"] and s["detail"][0]["avail"] == "◐ u dodavatele"


def test_printed_supplier_stock_beats_out_of_stock_in_structured_data():
    """bikero: JSON-LD says OutOfStock for drop-shipped goods, the page prints 'Skladem u dodavatele'."""
    page = """<html><head><script type="application/ld+json">{"@type":"Product","name":"Shimano RD-R8150",
    "offers":{"@type":"Offer","price":"6999","priceCurrency":"CZK","availability":"https://schema.org/%s"}}</script></head>
    <body><div class="AvailabilityInfo"> %s </div>%s</body></html>"""
    assert parse_product_page(page % ("OutOfStock", "Skladem u dodavatele", PAD), "u")[0].availability == "supplier"
    assert parse_product_page(page % ("OutOfStock", "Vyprodáno", PAD), "u")[0].availability == "out"
    assert parse_product_page(page % ("OutOfStock", "Skladem", PAD), "u")[0].availability == "out"         # only 'u dodavatele' corrects
    assert parse_product_page(page % ("InStock", "Skladem u dodavatele", PAD), "u")[0].availability == "in_stock"
    assert parse_product_page(page % ("BackOrder", "Skladem u dodavatele", PAD), "u")[0].availability == "supplier"


def test_nothing_in_stock_is_said_in_the_note():
    t = build(rows_with({"a": (700, "out"), "b": (800, "backorder")}))
    mn = as_dicts(t["minimum"])[0]
    assert mn["Min Kč bez DPH"] == "" and mn["Dodavatel CZK bez DPH"] == 1000
    assert mn["Poznámka"] == "nic skladem (2 nabídek mimo sklad, nejlevnější 700 Kč u a.test: vyprodáno)"
    assert t["min_rows"] == [] and len(t["over"]) == 1 and len(t["changes"]) == 1


def test_previous_run_is_compared_on_in_stock_offers_too():
    prev = rows_with({"a": (500, "out"), "b": (1000, "in_stock")})
    t = build(rows_with({"b": (900, "in_stock")}), prev)
    assert as_dicts(t["minimum"])[0]["Δ % vs. minulý běh"] == -10.0           # not +80 % against the sold-out 500


def test_switch_off_restores_all_offers():
    t = build(rows_with({"a": (700, "out"), "c": (950, "in_stock")}), settings=dict(SETTINGS, in_stock_only=False))
    assert as_dicts(t["minimum"])[0]["Min Kč bez DPH"] == 700


def test_dashboard_shows_off_shelf_offers_but_never_counts_them():
    rows = rows_with({"a": (700, "out"), "b": (800, "unknown"), "c": (950, "in_stock")})
    skus = [s for s in config.load_skus() if s.sku_id == SKU]
    ctx = dashboard.build_context(rows, skus, SHOPS, SETTINGS, FX, SUP)
    s = ctx["skus"][0]
    assert (s["status"], s["min_text"], s["min_shop"], s["diff_kc"]) == ("cheaper", "950 Kč", "c.test", "o 50 Kč levněji")
    assert [(d["shop"], d["kind"], d["rank"]) for d in s["detail"]] == [("c.test", "cheaper", "1. "), ("a.test", "nostock", ""),
                                                                        ("b.test", "nostock", "")]
    assert [c["cls"] for c in s["cells"]] == ["nostock", "nostock", ""] and s["cells"][2]["ring"]
    assert "1 platných nabídek, 2 mimo sklad" in s["detail_note"]
    assert ctx["kpi"]["savings"] == "50 Kč"
    assert any(p["shop"] == "b.test" and "neznámá dostupnost" in p["text"] for p in ctx["shop_problems"])

    html = dashboard.render(ctx)
    assert "jen skladem" in html and 'id="f-sklad" hidden' in html            # filter chip is pointless now

    none = dashboard.build_context(rows_with({"a": (700, "out")}), skus, SHOPS, SETTINGS, FX, SUP)["skus"][0]
    assert (none["status"], none["badge"], none["min_shop"]) == ("nostock", "× nic skladem", "1× mimo sklad, od 700 Kč")


def test_availability_from_nested_offers_of_an_aggregate():
    """starbike: the AggregateOffer has the price, its price-less nested offers the availability."""
    html = """<html><head><script type="application/ld+json">{"@type":"Product","name":"Shimano RD-R8150",
    "offers":{"@type":"AggregateOffer","lowPrice":250.0,"priceCurrency":"EUR","eligibleRegion":"DE",
    "offers":[{"sku":"1","availability":"https://schema.org/OutOfStock"},{"sku":"2","availability":"https://schema.org/InStock"}]}}
    </script></head><body>%s</body></html>""" % PAD
    offers = parse_product_page(html, "https://s/p")
    assert [(o.price, o.availability, o.region) for o in offers] == [(250.0, "in_stock", "DE")]


def test_availability_from_visible_text_when_structured_data_has_none():
    """hupnakolo (Shoptet): only <div class="availability">skladem</div> on the page."""
    page = """<html><head><meta property="og:title" content="Shimano RD-R8150"><meta property="product:price:amount" content="6296">
    <meta property="product:price:currency" content="CZK"></head><body><div class="p-detail"><div class="availability">
    <span style="color:#32cb00"> %s </span></div></div><div class="related"><div class="availability">skladem</div></div>%s</body></html>"""
    assert parse_product_page(page % ("Skladem v eshopu", PAD), "u")[0].availability == "in_stock"
    assert parse_product_page(page % ("Není skladem", PAD), "u")[0].availability == "out"          # product block wins
    assert parse_product_page(page % ("skladem u dodavatele (5-7 dní)", PAD), "u")[0].availability == "supplier"
    assert parse_product_page(page % ("Na objednávku", PAD), "u")[0].availability == "backorder"

    for text, want in [("skladem", "in_stock"), ("Vyprodáno", "out"), ("Na objednávku", "backorder"), ("Předobjednávka", "preorder"),
                       ("Skladem u dodavatele", "supplier"), ("Auf Lager", "in_stock"), ("Nicht lieferbar", "out"), ("In stock", "in_stock"), ("Out of stock", "out"),
                       ("Poslední kus", "limited"), ("", "unknown"), ("Doprava zdarma", "unknown")]:
        assert availability_from_text(text) == want, text
