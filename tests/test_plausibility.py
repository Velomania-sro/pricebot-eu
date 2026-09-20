"""Offers more than implausible_below_pct % under the reference price are a different article -> dropped."""
import pricebot.__main__ as cli
from pricebot import config, fetch, report, sitemap
from pricebot.config import Shop
from pricebot.fetch import Page
from pricebot.parse import Offer
from pricebot.resolve import resolve
from tests.test_pipeline import PAD, product
from tests.test_supplier import FX, SHOPS, as_dicts, offer

SETTINGS = dict(config.load_settings(), implausible_below_pct=40, supplier_threshold_pct=10)
SKU = "SH-ULT-RD"


def flag(rows, supplier):
    return report.flag_implausible(rows, supplier, SETTINGS, FX["rate"])


def test_supplier_reference_more_than_40_pct_below_is_dropped():
    rows = [offer(SKU, "a", 599), offer(SKU, "b", 600), offer(SKU, "c", 950)]      # supplier 1000 -> floor 600
    dropped = flag(rows, {SKU: {"net_czk": 1000.0, "note": ""}})
    assert [r["shop_id"] for r in dropped] == ["a"]                                # exactly -40 % stays
    assert rows[0]["status"] == "implausible" and rows[1]["status"] == rows[2]["status"] == "ok"
    assert "-40.1 % vs cena dodavatele 1000 Kč" in rows[0]["flag"]
    assert rows[0]["price_czk_net"] == 599                                         # price kept for Detail


def test_median_of_other_shops_is_the_reference_without_supplier_price():
    rows = [offer(SKU, "a", 120), offer(SKU, "b", 5000), offer(SKU, "c", 5400)]
    assert [r["shop_id"] for r in flag(rows, {})] == ["a"]
    assert "medián ostatních shopů 5200 Kč" in rows[0]["flag"]

    two = [offer(SKU, "a", 120), offer(SKU, "b", 5000)]                            # one other shop is no reference
    assert flag(two, {}) == [] and all(r["status"] == "ok" for r in two)

    junk = [offer(SKU, "a", 100), offer(SKU, "b", 120), offer(SKU, "c", 5000), offer(SKU, "d", 5400)]
    assert sorted(r["shop_id"] for r in flag(junk, {})) == ["a", "b"]              # judged before anything is marked


def test_disabled_with_zero_pct():
    rows = [offer(SKU, "a", 10), offer(SKU, "b", 5000), offer(SKU, "c", 5400)]
    assert report.flag_implausible(rows, {}, dict(SETTINGS, implausible_below_pct=0), FX["rate"]) == []


def test_dropped_offer_leaves_main_sheets_and_history_but_stays_in_detail():
    rows = [offer(SKU, "a", 123), offer(SKU, "b", 950)]
    supplier = {SKU: {"net_czk": 1000.0, "note": ""}}
    flag(rows, supplier)
    skus = [s for s in config.load_skus() if s.sku_id == SKU]
    t = report.build(rows, skus, SHOPS, [], {}, SETTINGS, FX, supplier)

    m = dict(zip(t["matrix"][0], t["matrix"][1]))
    assert (m["Min Kč bez DPH"], m["Nejlevnější shop"], m["a.test"], m["b.test"]) == (950, "b.test", "", 950)
    assert as_dicts(t["minimum"])[0]["Úspora CZK"] == 50
    assert len(t["over"]) == 1
    assert [r["shop_id"] for r in t["min_rows"]] == ["b"]
    det = next(d for d in as_dicts(t["detail"]) if d["Shop"] == "a.test")
    assert det["Stav"] == "podezřele nízká cena" and det["Kč bez DPH"] == 123 and "jiný sortiment" in det["Poznámka"]


def test_polluted_history_is_not_used_for_deltas():
    """Yesterday's minimum and the 30-day minimum were a 123 Kč spare part: no -/+ thousands of % today."""
    supplier = {SKU: {"net_czk": 1000.0, "note": ""}}
    rows = [offer(SKU, "b", 950)]
    prev = [offer(SKU, "a", 123), offer(SKU, "b", 1000)]
    skus = [s for s in config.load_skus() if s.sku_id == SKU]
    t = report.build(rows, skus, SHOPS, prev, {SKU: 123 / FX["rate"]}, SETTINGS, FX, supplier)
    mn = as_dicts(t["minimum"])[0]
    assert mn["Δ % vs. minulý běh"] == -5.0 and mn["Δ % vs. 30d min"] == ""


def test_plausible_fn_converts_shop_price_to_czk_net():
    sku = {s.sku_id: s for s in config.load_skus()}[SKU]
    de = Shop(id="d", name="d", country="DE", currency="EUR", vat=0.19, search_url="https://d/{q}")
    rates = {"EUR": 1.0, "CZK": 25.0}
    ok = cli._plausible_fn(sku, de, rates, {SKU: {"net_czk": 5000.0}}, SETTINGS)    # floor 3000 Kč net
    assert ok(Offer("x", 142.80, "EUR", "in_stock", "u", "jsonld"))                 # 142.80 / 1.19 * 25 = 3000
    assert not ok(Offer("x", 142.0, "EUR", "in_stock", "u", "jsonld"))
    assert ok(Offer("x", 3630.0, "CZK", "in_stock", "u", "jsonld")) is True         # 3630 / 25 / 1.19 * 25 = 3050
    assert cli._plausible_fn(sku, de, rates, {}, SETTINGS) is None                  # no supplier price -> no check
    assert cli._plausible_fn(sku, de, rates, {SKU: {"net_czk": 5000.0}}, dict(SETTINGS, implausible_below_pct=0)) is None


def test_resolve_skips_implausibly_cheap_candidate(monkeypatch):
    """kupkolo-style: the RD-R9250 cable guide (288 Kč) and the derailleur share the part number."""
    guide = "https://k.test/shimano-rd-r8150-ultegra-di2-vnejsi-voditko_z111/"
    real = "https://k.test/prehazovacka-shimano-ultegra-rd-r8150-di2_z222/"

    def get(self, url):
        if "/search" in url:
            return Page(200, "<html><body>nic</body></html>" + PAD, url)
        if url == guide:
            return Page(200, product("Shimano Ultegra Di2 RD-R8150 12s", 349.0, "CZK"), url)
        return Page(200, product("Přehazovačka Shimano Ultegra Di2 RD-R8150 12s", 6296.0, "CZK"), url)

    monkeypatch.setattr(fetch.Fetcher, "get", get)
    monkeypatch.setattr(fetch.Fetcher, "_wait", lambda self: None)
    monkeypatch.setattr(sitemap, "collect_urls", lambda *a, **k: [guide, real])
    shop = Shop(id="k", name="k.test", country="CZ", currency="CZK", vat=0.21, search_url="https://k.test/search?q={q}")
    sku = {s.sku_id: s for s in config.load_skus()}[SKU]
    settings = config.load_settings()

    found = resolve(sku, shop, fetch.Fetcher(shop, settings), settings, log=lambda m: None)
    assert found[0].price == 349.0                                                  # without the check the cheap one wins

    ok = cli._plausible_fn(sku, shop, {"EUR": 1.0, "CZK": 25.0}, {SKU: {"net_czk": 5000.0}}, SETTINGS)
    found = resolve(sku, shop, fetch.Fetcher(shop, settings), settings, log=lambda m: None, plausible=ok)
    assert found[0].price == 6296.0 and found[1] == real
