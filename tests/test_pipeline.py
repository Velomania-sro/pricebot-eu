"""End-to-end run against a fake shop served from memory (no network)."""
import csv
import json

import pytest

import pricebot.__main__ as cli
from pricebot import fetch, fx, store
from pricebot.config import Shop
from pricebot.fetch import Page

PAD = "<p>" + "x" * 7000 + "</p>"


def product(name, price, cur="EUR", avail="InStock"):
    return f"""<html><head><script type="application/ld+json">
    {{"@type":"Product","name":"{name}","offers":{{"@type":"Offer","price":"{price}","priceCurrency":"{cur}",
    "availability":"https://schema.org/{avail}"}}}}</script></head><body>{PAD}</body></html>"""


class FakeShop:
    """Search page lists anchors; product pages carry JSON-LD. Prices can be changed between runs."""

    def __init__(self):
        self.products = {
            "/p/rd-r8150": ("Shimano Ultegra Di2 RD-R8150 12-speed Rear Derailleur", 279.0),
            "/p/rd-r8150-pulley": ("Shimano RD-R8150 Pulley Set", 29.0),
            "/p/group": ("Shimano Ultegra Di2 R8170 Disc Groupset incl. RD-R8150", 1899.0),
            "/p/cs-r8100": ("Shimano Ultegra CS-R8100 Cassette 12-speed 11-30", 99.0),
        }
        self.hits = []

    def get(self, url):
        self.hits.append(url)
        path = url.split("fake.test", 1)[1]
        if path.startswith("/search"):
            q = path.split("q=")[1].lower()
            links = "".join(f'<a href="{p}">{n}</a>' for p, (n, _) in self.products.items() if q.replace("+", " ") in n.lower())
            return Page(200, f"<html><body>{links}{PAD}</body></html>", url)
        if path in self.products:
            n, pr = self.products[path]
            return Page(200, product(n, pr), url)
        return Page(404, "not found", url)


@pytest.fixture
def env(tmp_path, monkeypatch):
    data = tmp_path / "data"
    monkeypatch.setattr(store, "URLS", data / "urls.json")
    monkeypatch.setattr(store, "LATEST", data / "latest.json")
    monkeypatch.setattr(store, "HISTORY_DIR", data / "history")
    monkeypatch.setattr(store, "HISTORY_MIN", data / "history_min.csv")
    monkeypatch.setattr(cli, "DATA", data)
    monkeypatch.setattr(fx, "get_rates", lambda cache=None: {"EUR": 1.0, "CZK": 25.0})
    monkeypatch.setattr(cli, "get_rates", lambda: {"EUR": 1.0, "CZK": 25.0})
    shop = FakeShop()
    monkeypatch.setattr(fetch.Fetcher, "get", lambda self, url: shop.get(url))
    monkeypatch.setattr(fetch.Fetcher, "_wait", lambda self: None)
    fake = Shop(id="fake", name="fake.test", country="DE", currency="EUR", vat=0.19,
                search_url="https://fake.test/search?q={q}")
    monkeypatch.setattr(cli, "load_shops", lambda: [fake])
    monkeypatch.setattr(cli, "sheet", type("S", (), {"push": staticmethod(lambda *a, **k: False)}))
    return shop, data


def test_run_twice_detects_price_drop(env):
    shop, data = env
    rc = cli.main(["run", "--skus", "SH-ULT-RD,SH-ULT-CS,SH-ULT-FD", "--no-sheet"])
    assert rc == 0

    latest = json.loads((data / "latest.json").read_text())["rows"]
    by = {r["sku_id"]: r for r in latest}
    assert by["SH-ULT-RD"]["status"] == "ok"
    assert by["SH-ULT-RD"]["price_local"] == 279.0
    assert by["SH-ULT-RD"]["price_eur_net"] == round(279 / 1.19, 2)
    assert by["SH-ULT-RD"]["url"].endswith("/p/rd-r8150")         # not the pulley, not the groupset
    assert by["SH-ULT-CS"]["status"] == "ok"
    assert by["SH-ULT-FD"]["status"] == "not_found"

    urls = json.loads((data / "urls.json").read_text())
    assert urls["SH-ULT-RD"]["fake"]["url"].endswith("/p/rd-r8150")

    matrix = list(csv.reader((data / "matrix.csv").open(encoding="utf-8")))
    assert matrix[0][-1] == "fake.test"
    rd_row = next(r for r in matrix if r[0] == "SH-ULT-RD")
    assert rd_row[7] == rd_row[-1] == str(round(279 / 1.19, 2))

    # second run: cached URL is used (no search), price dropped 10 % -> change row
    shop.products["/p/rd-r8150"] = ("Shimano Ultegra Di2 RD-R8150 12-speed Rear Derailleur", 251.0)
    shop.hits.clear()
    cli.main(["run", "--skus", "SH-ULT-RD,SH-ULT-CS", "--no-sheet"])
    assert not any("/search" in h for h in shop.hits)
    changes = list(csv.reader((data / "changes.csv").open(encoding="utf-8")))
    rd_change = next(r for r in changes[1:] if r[1] == "SH-ULT-RD")
    assert "pokles ceny" in rd_change[3]
    assert float(rd_change[8]) == pytest.approx(-10.0, abs=0.1)

    hist = list(csv.DictReader((data / "history_min.csv").open(encoding="utf-8")))
    assert len([h for h in hist if h["sku_id"] == "SH-ULT-RD"]) == 2


def test_sheet_push_failure_does_not_crash_run(env, monkeypatch):
    """A Google Sheet write blowing up must not fail the run; data/ is already written."""
    shop, data = env

    def boom(*a, **k):
        raise RuntimeError("Sheets API 503")

    monkeypatch.setattr(cli, "sheet", type("S", (), {"push": staticmethod(boom)}))
    rc = cli.main(["run", "--skus", "SH-ULT-RD"])          # note: NO --no-sheet -> push is attempted
    assert rc == 0                                          # exception swallowed, run reported success
    assert (data / "latest.json").exists()                 # data still persisted despite Sheet failure
    assert (data / "matrix.csv").exists()


def test_multi_template_fallback_and_overrides(tmp_path, monkeypatch):
    """First template returns nothing, second works -> resolve must fall through to it."""
    from pricebot import config
    from pricebot.fetch import Page
    from pricebot.resolve import resolve

    class TwoTemplateShop:
        def __init__(self):
            self.seen = []

        def get(self, url):
            self.seen.append(url)
            if "/bad?" in url:
                return Page(200, "<html><body>nic</body></html>" + PAD, url)
            if "/good?" in url:
                return Page(200, '<html><body><a href="/p/rd">Shimano Ultegra Di2 RD-R8150 Rear Derailleur</a></body></html>' + PAD, url)
            return Page(200, product("Shimano Ultegra Di2 RD-R8150 Rear Derailleur", 269.0), url)

    fake = TwoTemplateShop()
    monkeypatch.setattr(fetch.Fetcher, "get", lambda self, url: fake.get(url))
    monkeypatch.setattr(fetch.Fetcher, "_wait", lambda self: None)
    shop = Shop(id="two", name="two.test", country="DE", currency="EUR", vat=0.19,
                search_url=["https://two.test/bad?q={q}", "https://two.test/good?q={q}"])
    assert shop.search_urls == ["https://two.test/bad?q={q}", "https://two.test/good?q={q}"]

    sku = {s.sku_id: s for s in config.load_skus()}["SH-ULT-RD"]
    f = fetch.Fetcher(shop, config.load_settings())
    found = resolve(sku, shop, f, config.load_settings(), log=lambda m: None)
    assert found is not None
    offer, url = found
    assert offer.price == 269.0 and url.endswith("/p/rd")
    assert any("/bad?" in u for u in fake.seen) and any("/good?" in u for u in fake.seen)


def test_overrides_roundtrip(tmp_path):
    from pricebot.config import load_overrides, save_overrides

    path = tmp_path / "shops.local.yaml"
    save_overrides({"bike24": {"search_url": "https://x/{q}", "fetcher": "playwright", "verified": True}}, path)
    got = load_overrides(path)
    assert got == {"bike24": {"search_url": "https://x/{q}", "fetcher": "playwright", "verified": True}}


def test_search_url_without_placeholder_is_rejected():
    shop = Shop(id="x", name="x", country="DE", currency="EUR", vat=0.19, search_url="https://x/search")
    with pytest.raises(ValueError):
        shop.search_urls


def test_unknown_currency_falls_back_to_shop_currency():
    from pricebot.__main__ import _ok_row
    from pricebot.config import Shop, load_skus
    from pricebot.parse import Offer

    sku = {s.sku_id: s for s in load_skus()}["SH-ULT-RD"]
    shop = Shop(id="s", name="s", country="DE", currency="EUR", vat=0.19, search_url="https://s/{q}")
    rates = {"EUR": 1.0, "CZK": 25.0}

    row = _ok_row(sku, shop, "2026-08-23", "ts", Offer("RD-R8150", 299.0, "", "in_stock", "u", "jsonld"), "u", rates)
    assert row["status"] == "ok" and row["currency"] == "EUR" and row["price_eur_net"] == round(299 / 1.19, 2)

    row = _ok_row(sku, shop, "2026-08-23", "ts", Offer("RD-R8150", 299.0, "XYZ", "in_stock", "u", "jsonld"), "u", rates)
    assert row["status"] == "ok" and row["currency"] == "EUR"
    assert "neznámá měna" in row["flag"]

    row = _ok_row(sku, shop, "2026-08-23", "ts", Offer("RD-R8150", 7000.0, "CZK", "in_stock", "u", "jsonld"), "u", rates)
    assert "jiná měna" in row["flag"] and row["price_eur"] == 280.0
