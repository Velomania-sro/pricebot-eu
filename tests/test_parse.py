import re

from pricebot.config import load_skus
from pricebot.parse import best_offer, parse_price, parse_product_page, search_candidates
from pricebot.resolve import prefilter_score, title_ok

SKUS = {s.sku_id: s for s in load_skus()}


def test_parse_price_formats():
    assert parse_price("1.299,00 €") == 1299.0
    assert parse_price("€1,299.00") == 1299.0
    assert parse_price("12 990 Kč") == 12990.0
    assert parse_price("1299") == 1299.0
    assert parse_price("1,299") == 1299.0
    assert parse_price("299,95") == 299.95
    assert parse_price(None) is None
    assert parse_price("n/a") is None


JSONLD_AGG = """<html><head><title>x</title>
<script type="application/ld+json">{"@context":"https://schema.org","@type":"Product",
 "name":"Shimano Ultegra Di2 FC-R8100 12-speed Crankset",
 "offers":{"@type":"AggregateOffer","lowPrice":"329.90","highPrice":"349.90","priceCurrency":"EUR",
 "offers":[{"@type":"Offer","price":"329.90","priceCurrency":"EUR","availability":"https://schema.org/InStock","name":"50-34 172.5"},
           {"@type":"Offer","price":"349.90","priceCurrency":"EUR","availability":"https://schema.org/OutOfStock","name":"52-36 175"}]}}
</script></head><body>long page %s</body></html>""" % ("x" * 7000)

JSONLD_GRAPH_CZ = """<html><head>
<script type="application/ld+json">{"@context":"https://schema.org","@graph":[
 {"@type":"BreadcrumbList"},
 {"@type":"Product","name":"Přehazovačka Shimano Ultegra Di2 RD-R8150 12 rychl.",
  "offers":{"@type":"Offer","price":"8 490","priceCurrency":"CZK","availability":"http://schema.org/InStock"}}]}
</script></head><body></body></html>"""

META_ONLY = """<html><head><title>SRAM Force AXS Rear Derailleur 2025 | Shop</title>
<meta property="og:title" content="SRAM Force AXS Rear Derailleur 2025">
<meta property="product:price:amount" content="389.00"><meta property="product:price:currency" content="EUR">
<meta property="product:availability" content="instock"></head><body></body></html>"""


def test_jsonld_aggregate_and_variants():
    offers = parse_product_page(JSONLD_AGG, "https://x/p1")
    prices = sorted(o.price for o in offers)
    assert prices == [329.9, 329.9, 349.9]          # aggregate lowPrice + two variant offers
    best = best_offer(offers)
    assert best.price == 329.9 and best.availability == "in_stock" and best.currency == "EUR"


def test_jsonld_graph_czech_price():
    offers = parse_product_page(JSONLD_GRAPH_CZ, "https://x/p2")
    assert len(offers) == 1
    assert offers[0].price == 8490.0 and offers[0].currency == "CZK"
    assert title_ok(offers[0].name, SKUS["SH-ULT-RD"])


def test_meta_fallback():
    offers = parse_product_page(META_ONLY, "https://x/p3")
    assert offers and offers[0].price == 389.0 and offers[0].source == "meta"
    assert title_ok(offers[0].name, SKUS["SR-FORCE-RD"])


SEARCH_ITEMLIST = """<html><head><script type="application/ld+json">
{"@type":"ItemList","itemListElement":[
 {"@type":"ListItem","position":1,"url":"/en/Shimano/Ultegra-Di2-RD-R8150-Rear-Derailleur-p84001/","name":"Shimano Ultegra Di2 RD-R8150 Rear Derailleur"},
 {"@type":"ListItem","position":2,"item":{"@type":"Product","url":"https://www.shop.de/en/Shimano/RD-R8150-Pulley-p999/","name":"Shimano RD-R8150 Pulley Set"}}]}
</script></head><body></body></html>"""

SEARCH_ANCHORS = """<html><body>
<a href="/en/Shimano/Ultegra-Di2-RD-R8150-p84001/">Shimano Ultegra Di2 RD-R8150 12-speed Rear Derailleur</a>
<a href="/en/Shimano/Ultegra-Di2-Groupset-p84002/">Shimano Ultegra Di2 R8170 Groupset incl. RD-R8150</a>
<a href="/en/cart/">Cart</a>
<a href="https://www.other.com/RD-R8150">external</a>
</body></html>"""


def test_search_candidates_itemlist_and_ranking():
    sku = SKUS["SH-ULT-RD"]
    cands = search_candidates(SEARCH_ITEMLIST, "https://www.shop.de/en/s/?keywords=RD-R8150", None, sku.must_match[0])
    assert [u for u, _ in cands] == [
        "https://www.shop.de/en/Shimano/Ultegra-Di2-RD-R8150-Rear-Derailleur-p84001/",
        "https://www.shop.de/en/Shimano/RD-R8150-Pulley-p999/",
    ]
    scores = [prefilter_score(t, sku) for _, t in cands]
    assert scores[0] > scores[1]                      # pulley set is penalised by must_not_match


def test_search_candidates_anchor_fallback_and_pattern():
    sku = SKUS["SH-ULT-RD"]
    pat = re.compile(r"shop\.de/en/[^/?#]+/[^/?#]+-p\d+/", re.I)
    cands = search_candidates(SEARCH_ANCHORS, "https://www.shop.de/en/s/", pat, sku.must_match[0])
    assert len(cands) == 2 and all("shop.de" in u for u, _ in cands)
    cands2 = search_candidates(SEARCH_ANCHORS, "https://www.shop.de/en/s/", None, sku.must_match[0])
    assert len(cands2) == 2                           # heuristic: key token in title/href, external host dropped


def test_title_rules_realistic():
    ok = title_ok
    # Shimano
    assert ok("Shimano Ultegra Di2 RD-R8150 12-speed Rear Derailleur", SKUS["SH-ULT-RD"])
    assert not ok("Shimano Ultegra Di2 R8170 Groupset incl. RD-R8150", SKUS["SH-ULT-RD"])
    assert not ok("Shimano RD-R8150 Pulley Set", SKUS["SH-ULT-RD"])
    assert ok("Shimano Ultegra FC-R8100 Kurbelgarnitur 2x12 50-34 172,5 mm", SKUS["SH-ULT-FC"])
    assert not ok("Shimano Ultegra FC-R8100-P Power Meter Crankset", SKUS["SH-ULT-FC"])
    assert ok("Shimano Ultegra FC-R8100-P Power Meter Crankset 52-36", SKUS["SH-ULT-FC-PM"])
    assert not ok("Shimano Ultegra FC-R8100 Kettenblatt 50T", SKUS["SH-ULT-FC"])
    assert ok("Řetěz Shimano XT CN-M8100 12 rychl. + spojka", SKUS["SH-ULT-CN"])
    assert ok("Shimano Ultegra Di2 R8170 Disc Groupset 2x12 50-34 11-30", SKUS["SH-ULT-SET"])
    assert not ok("Shimano Ultegra Di2 R8170 Disc Groupset with FC-R8100-P Power Meter", SKUS["SH-ULT-SET"])
    assert ok("Shimano Ultegra Di2 R8170 Disc Groupset with FC-R8100-P Power Meter", SKUS["SH-ULT-SET-PM"])
    assert ok("Shimano 105 Di2 R7170 sada komponentů 2x12 kotoučové", SKUS["SH-105-SET"])
    assert ok("Shimano Dura-Ace Di2 R9270 Groupset 2x12 Disc", SKUS["SH-DA-SET"])
    assert ok("Shimano Dura Ace Di2 R9270 Gruppe", SKUS["SH-DA-SET"])
    assert ok("Shimano Ultegra ST-R8170 + BR-R8170 Di2 Brake/Shift Set front+rear", SKUS["SH-ULT-STBR"])
    assert not ok("Shimano ST-R8170 Bracket Cover / Hoods", SKUS["SH-ULT-STBR"])
    assert ok("Shimano RT-CL800 Brake Rotor Center Lock 160 mm", SKUS["SH-X-RT-CL800"])
    assert not ok("Shimano RT-CL800 Brake Rotor 160 mm - 6-bolt", SKUS["SH-X-RT-CL800"])
    # SRAM
    assert ok("SRAM Force AXS Rear Derailleur 2025", SKUS["SR-FORCE-RD"])
    assert not ok("SRAM Force AXS D2 Rear Derailleur", SKUS["SR-FORCE-RD"])
    assert not ok("SRAM Force XPLR AXS Rear Derailleur 13-speed", SKUS["SR-FORCE-RD"])
    assert not ok("SRAM Force eTap AXS Schaltwerk", SKUS["SR-FORCE-RD"])
    assert ok("SRAM Red AXS Shift-Brake System rear/right E1", SKUS["SR-RED-STBR"])
    assert not ok("SRAM Red eTap AXS HRD Shift-Brake Lever", SKUS["SR-RED-STBR"])
    assert ok("SRAM Rival AXS Crankset 2x12 46-33 DUB 172.5 2025", SKUS["SR-RIVAL-FC"])
    assert not ok("SRAM Rival AXS Power Meter Crankset 2x12 46-33", SKUS["SR-RIVAL-FC"])
    assert ok("SRAM Rival AXS Power Meter Crankset 2x12 46-33", SKUS["SR-RIVAL-FC-PM"])
    assert ok("SRAM XG-1270 Kassette 12-fach 10-30", SKUS["SR-FORCE-CS"])
    assert not ok("SRAM XG-1371 XPLR Cassette 13-speed 10-46", SKUS["SR-FORCE-CS"])
    assert ok("SRAM Force AXS 2x12 Groupset 2025 48-35 10-33", SKUS["SR-FORCE-SET"])
    assert not ok("SRAM Force AXS 2x12 Groupset 2025 Power Meter 48-35", SKUS["SR-FORCE-SET"])
    assert ok("SRAM Force AXS 2x12 Groupset 2025 Power Meter 48-35", SKUS["SR-FORCE-SET-PM"])
    assert ok("SRAM Force AXS D2 2x12 Groupset 2023", SKUS["SR-FORCE-D2-SET"])
    assert not ok("SRAM Force AXS 2x12 Groupset 2025", SKUS["SR-FORCE-D2-SET"])
    assert ok("SRAM Rival eTap AXS Disc Groupset 2x12", SKUS["SR-RIVAL-D1-SET"])
    assert ok("SRAM AXS Battery", SKUS["SR-X-BATTERY"])
    assert not ok("SRAM AXS Battery Charger and Cord", SKUS["SR-X-BATTERY"])
    assert ok("SRAM AXS Battery Charger and Cord", SKUS["SR-X-CHARGER"])
    assert ok("SRAM Paceline Rotor 160 mm Center Lock", SKUS["SR-X-PACELINE-160"])
    assert not ok("SRAM Paceline X Rotor 160 mm Center Lock", SKUS["SR-X-PACELINE-160"])
    assert ok("SRAM Paceline X Rotor 160 mm Center Lock", SKUS["SR-X-PACELINE-X-160"])


# --------------------------------------------------------------- sitemap fallback
SITEMAP_INDEX = """<?xml version="1.0"?><sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
<sitemap><loc>https://shop.de/sitemap_products_1.xml</loc></sitemap>
<sitemap><loc>https://shop.de/sitemap_blog.xml</loc></sitemap></sitemapindex>"""

SITEMAP_PRODUCTS = """<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
<url><loc>https://shop.de/en/shimano-ultegra-di2-rd-r8150-12-speed-rear-derailleur</loc></url>
<url><loc>https://shop.de/en/shimano-ultegra-rd-r8150-pulley-set</loc></url>
<url><loc>https://shop.de/en/shimano-ultegra-di2-r8170-groupset</loc></url>
<url><loc>https://shop.de/clanek/186/jak-prezit-na-kole</loc></url>
<url><loc>https://shop.de/en/sram-force-axs-2025-rear-derailleur</loc></url></urlset>"""


def test_sitemap_candidates_match_slugs():
    from pricebot import sitemap

    urls = [u for u in re.findall(r"<loc>([^<]+)</loc>", SITEMAP_PRODUCTS)
            if not sitemap._SKIP_RE.search(u)]
    assert "https://shop.de/clanek/186/jak-prezit-na-kole" not in urls   # blog filtered out

    hits = sitemap.candidates(SKUS["SH-ULT-RD"], urls)
    assert [u for u, _ in hits] == ["https://shop.de/en/shimano-ultegra-di2-rd-r8150-12-speed-rear-derailleur"]

    hits = sitemap.candidates(SKUS["SR-FORCE-RD"], urls)
    assert [u for u, _ in hits] == ["https://shop.de/en/sram-force-axs-2025-rear-derailleur"]

    assert sitemap._deslug("https://shop.de/en/prehazovacka-rd-r8150.html") == "en prehazovacka rd r8150"


def test_sitemap_collect_follows_index(tmp_path, monkeypatch):
    from pricebot import sitemap
    from pricebot.config import Shop

    monkeypatch.setattr(sitemap, "CACHE", tmp_path / "sitemaps")
    pages = {
        "https://shop.de/robots.txt": "User-agent: *\nSitemap: https://shop.de/sitemap.xml\n",
        "https://shop.de/sitemap.xml": SITEMAP_INDEX,
        "https://shop.de/sitemap_products_1.xml": SITEMAP_PRODUCTS,
    }

    class FakeResp:
        def __init__(self, text):
            self.content = text.encode()

        def raise_for_status(self):
            pass

    class FakeSession:
        def get(self, url, timeout=None):
            if url not in pages:
                raise RuntimeError(f"404 {url}")
            return FakeResp(pages[url])

    shop = Shop(id="s", name="shop.de", country="DE", currency="EUR", vat=0.19,
                search_url="https://shop.de/search?q={q}")
    settings = {"timeout": 10, "sitemap_max_files": 40, "sitemap_max_age_days": 7}
    urls = sitemap.collect_urls(shop, FakeSession(), settings, log=lambda m: None)
    assert "https://shop.de/en/shimano-ultegra-di2-rd-r8150-12-speed-rear-derailleur" in urls
    assert not any("/clanek/" in u for u in urls)
    # second call must come from cache (session would raise on the blog sitemap otherwise)
    assert sitemap.collect_urls(shop, FakeSession(), settings, log=lambda m: None) == urls


def test_facet_links_are_not_candidates():
    """rosebikes-style: search page links back to itself with category filters - not products."""
    html = """<html><body>
    <a href="/search?query=RD-R8150&category%5B%5D=2">Fahrräder (119)</a>
    <a href="/search?query=RD-R8150&category%5B%5D=164">Fahrradteile (4165)</a>
    <a href="/shimano-ultegra-di2-rd-r8150-schaltwerk/aid:1234">Shimano Ultegra Di2 RD-R8150 Schaltwerk</a>
    </body></html>"""
    cands = search_candidates(html, "https://www.rosebikes.de/search?query=RD-R8150", None,
                              SKUS["SH-ULT-RD"].must_match[0])
    assert [u for u, _ in cands] == ["https://www.rosebikes.de/shimano-ultegra-di2-rd-r8150-schaltwerk/aid:1234"]


def test_sitemap_fallback_paths_when_robots_has_none(tmp_path, monkeypatch):
    """alltricks-style: robots.txt lists no sitemap and /sitemap.xml is 404 -> try other paths."""
    from pricebot import sitemap
    from pricebot.config import Shop

    monkeypatch.setattr(sitemap, "CACHE", tmp_path / "sm")
    pages = {"https://x.com/sitemap_index.xml": SITEMAP_PRODUCTS}
    tried: list[str] = []

    class R:
        def __init__(self, t):
            self.content = t.encode()

        def raise_for_status(self):
            pass

    class S:
        def get(self, url, timeout=None):
            tried.append(url)
            if url not in pages:
                raise RuntimeError("404")
            return R(pages[url])

    shop = Shop(id="x", name="x", country="FR", currency="EUR", vat=0.20,
                search_url="https://x.com/search?q={q}")
    urls = sitemap.collect_urls(shop, S(), {"timeout": 5, "sitemap_max_files": 150, "sitemap_max_age_days": 7},
                                log=lambda m: None)
    assert any("rd-r8150" in u for u in urls)
    assert "https://x.com/sitemap.xml" in tried and "https://x.com/sitemap_index.xml" in tried


def test_shop_cookies_and_headers_are_applied():
    from pricebot.config import Shop, load_settings
    from pricebot.fetch import Fetcher

    shop = Shop(id="s", name="s", country="DE", currency="EUR", vat=0.19,
                search_url="https://s/{q}", cookies={"currency": "EUR"}, headers={"X-Test": "1"})
    f = Fetcher(shop, load_settings())
    try:
        assert f.session.cookies.get("currency") == "EUR"
        assert f.session.headers["X-Test"] == "1"
    finally:
        f.close()
