# pricebot-eu – monitor cen Shimano Di2 / SRAM AXS na EU e-shopech

Denně projde vybrané evropské e-shopy, najde cenu každého SKU z `skus.csv`, přepočte ji na
**EUR bez DPH podle země shopu**, uloží historii a zapíše matici *SKU × shop* do Google Sheetu.
Běží zdarma na GitHub Actions (cron), nebo ručně z počítače.

Rozsah master listu (65 SKU, `skus.csv`):

| Značka | Řady | Co se sleduje |
|---|---|---|
| Shimano | 105 Di2 R7100, Ultegra Di2 R8100, Dura-Ace Di2 R9200 | páky+třmeny, RD, FD, kliky (± wattmetr), kazety, řetěz, kompletní sady (± wattmetr), baterie, nabíjecí kabel, kotouče RT-CL800/900 |
| SRAM | Red AXS E1 (2024), Force AXS 2025, Rival AXS 2025 | shift-brake system, RD, FD, kliky (± Quarq), kazety XG-12x0, řetěz, kompletní sady (± wattmetr), baterie, nabíječka, kotouče Paceline / Centerline XR |
| SRAM – dobíhající | Force D2 (2023), Rival eTap D1, Red eTap D1 | kompletní sady (tam se objevují výprodeje) |

Vše 12rychlostní, silniční, kotoučové, elektronické. UK shopy jsou záměrně vynechané (clo + dovozní DPH).

---

## 1. Jak to funguje

```
skus.csv  ──┐                 ┌─ vyhledávání v shopu (search_url) → kandidáti → produktové stránky
shops.yaml ─┴─ run ──► shop ──┤                                                       │
                              └─ cache URL (data/urls.json) ──► produktová stránka ◄──┘
                                                                       │
                               JSON-LD Product/Offer → meta tagy → (volitelně Claude API)
                                                                       │
                      kurz ECB → EUR s DPH → EUR bez DPH (sazba země) → historie + diff
                                                                       │
                                    data/*.csv  +  Google Sheet (Matice, Minimum, Detail, Změny, Historie min)
```

* **Identifikace produktu** – ne podle názvu "od oka", ale podle pravidel v `skus.csv`
  (`must_match` / `must_not_match` / `prefer`, regulární výrazy, viz kap. 5). Po prvním úspěšném
  nalezení se URL uloží do `data/urls.json` a další běhy už jen čtou produktovou stránku
  (1 request na SKU a shop). Když URL přestane fungovat nebo název přestane odpovídat pravidlům,
  agent hledá znovu.
* **Cena** – primárně ze strukturovaných dat `application/ld+json` (Product → Offer/AggregateOffer),
  které mají prakticky všechny větší shopy kvůli Google Shopping. Fallback na Open Graph/microdata
  meta tagy. Třetí fallback (volitelný) je Claude Haiku, který cenu vytáhne z textu stránky –
  zapíná se pouhým nastavením `ANTHROPIC_API_KEY`.
* **Normalizace** – kurzy ECB (frankfurter.dev, cache v `data/fx.json`), DPH podle země shopu
  (`vat` v `shops.yaml`). Cena bez DPH = cena s DPH / (1 + sazba) – to je částka, kterou platíš
  při nákupu na DIČ v režimu reverse charge.
* **Diff** – proti minulému běhu a proti 30denní historii minim. Do listu *Změny* jdou jen
  pohyby nad `alert_pct` (výchozí 3 %), změna nejlevnějšího shopu a nová 30denní minima.

## 2. Rychlý start lokálně

Zip stačí rozbalit kamkoliv – **git lokálně nepotřebuješ** (na GitHub to později nahraješ přes
GitHub Desktop nebo webové rozhraní, viz kap. 4). Po rozbalení zkontroluj, že jsi ve složce, kde
leží `skus.csv` a `shops.yaml` – Windows „Extrahovat vše" někdy vytvoří `pricebot-eu\pricebot-eu`,
pak je potřeba `cd pricebot-eu` ještě jednou.

### Windows (PowerShell)

Jednorázově nainstaluj Python 3.12+ z <https://www.python.org/downloads/> a v instalátoru
**zaškrtni „Add python.exe to PATH"**. Pak **zavři a znovu otevři PowerShell** (jinak se `python`
nenajde). Alternativa jedním příkazem: `winget install -e --id Python.Python.3.12`.

Příkazy zadávej **po jednom řádku** – PowerShell 5 nezná `&&` – a kopíruj jen samotné příkazy,
nikdy řádky začínající `PS C:\…>`:

```powershell
cd $env:USERPROFILE\Downloads\pricebot-eu
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt

# 1) ověř šablony vyhledávání shopů – NUTNÉ před prvním ostrým během
python -m pricebot probe --fix
python -m pricebot probe --shops bike24 --q "SRAM Force AXS"

# 2) zkušební běh bez Sheetu
python -m pricebot run --no-sheet --limit 5 -v

# 3) ostrý běh se Sheetem (nastavení viz kap. 3)
$env:GOOGLE_SERVICE_ACCOUNT_JSON = Get-Content .\service-account.json -Raw
$env:SHEET_ID = "1AbC...xyz"
python -m pricebot run
```

* Chyba `…running scripts is disabled…` u `Activate.ps1`: jednorázově povol skripty příkazem
  `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` a aktivaci spusť znovu. Nebo aktivaci
  úplně vynech a všude volej `.\.venv\Scripts\python` místo `python`.
* Pokud některému shopu nastavíš `fetcher: playwright`, doinstaluj prohlížeč:
  `python -m playwright install chromium`.

### Linux / macOS (bash)

```bash
cd pricebot-eu
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

python -m pricebot probe --fix                # všechny shopy, hledá "RD-R8150"
python -m pricebot run --no-sheet --limit 5 -v

export GOOGLE_SERVICE_ACCOUNT_JSON="$(cat service-account.json)"
export SHEET_ID="1AbC...xyz"
python -m pricebot run
```

Užitečné varianty pro ladění (oba systémy):

```
python -m pricebot probe --shops koloshop --q "RD-R8150"
python -m pricebot run --no-sheet --shops bike-components,koloshop --skus SH-ULT-RD,SR-FORCE-RD -v
```

Co `probe` dělá: u každého shopu zkouší **postupně všechny šablony vyhledávání**, které jsou
u něj v `shops.yaml` (mám tam tři varianty na shop), a když ho shop odmítne jako bota, zkusí
tutéž šablonu ještě přes Playwright. Jakmile nějaká kombinace vrátí odkazy na produkty a na
produktové stránce najde cenu, označí ji za funkční:

```
=== [koloshop] koloshop.cz ==========================
  -   [requests] 0 odkazů na produkt (špatná šablona, JS výpis, nebo chybí product_url_pattern)
       https://www.koloshop.cz/vyhledavani/?q=RD-R8150
  OK  [requests] OK (7 kandidátů, cena ze zdroje 'jsonld')
       https://www.koloshop.cz/vyhledavani?string=RD-R8150
         - 'Přehazovačka Shimano Ultegra Di2 RD-R8150' -> https://www.koloshop.cz/...
       cena: 8490.0 CZK
  => funguje: https://www.koloshop.cz/vyhledavani?string={q}  (fetcher: requests)
```

S přepínačem `--fix` se funkční kombinace uloží do **`shops.local.yaml`** – ten má přednost před
`shops.yaml`, takže původní soubor i s komentáři zůstává nedotčený a smazáním `shops.local.yaml`
se vrátíš do výchozího stavu. Ostrý běh pak jede rovnou po ověřených šablonách.

Když neprojde ani jedna šablona:

| Hláška | Příčina | Řešení |
|---|---|---|
| `0 odkazů na produkt` u všech variant | jiná adresa vyhledávání | otevři vyhledávání shopu v prohlížeči, zkopíruj URL z adresního řádku a v `shops.yaml` nahraď hledaný výraz za `{q}` |
| `kandidáti ano, ale bez strukturované ceny` | cenu dopočítává JavaScript | u shopu `fetcher: playwright`, případně nech na Claude fallbacku (`ANTHROPIC_API_KEY`) |
| `BLOKOVÁNO` i přes Playwright | tvrdá bot ochrana (Cloudflare, DataDome) | `PRICEBOT_PROXY` (rezidenční proxy / scraping API), nebo shop vypni `enabled: false` |
| `0 odkazů`, ale v prohlížeči výsledky vidíš | odkazy na produkty se nedají poznat | doplň `product_url_pattern` – regex, který sedí na URL produktu (např. `nasekolo\.cz/produkt/`) |

Poznámka k Playwrightu: aby fungoval, musí být doinstalovaný prohlížeč
(`python -m playwright install chromium`). Bez něj `probe` u blokovaných shopů jen ohlásí chybu
a jede dál; vynechat pokusy o Playwright úplně jde přes `--no-playwright`.

Šablony `search_url` v `shops.yaml` jsou můj nejlepší odhad (proto jsou u každého shopu tři
varianty a příznak `verified: false`) – z prostředí, kde jsem skript psal, se na shopy nedalo
připojit, takže je ověří až tvůj první `probe --fix`.
Parsování produktových stránek je naproti tomu založené na standardu schema.org a je otestované offline
(`pytest`).

## 3. Google Sheet

1. Vytvoř prázdnou tabulku v Google Sheets. `SHEET_ID` je část URL mezi `/d/` a `/edit`.
2. Google Cloud Console → nový projekt → **APIs & Services → Enable**: *Google Sheets API* a *Google Drive API*.
3. **IAM → Service Accounts → Create**, pak **Keys → Add key → JSON** – stáhne se `service-account.json`.
4. Tabulku **nasdílej** e-mailu servisního účtu (`…@….iam.gserviceaccount.com`) jako *Editor*.
5. Obsah JSON souboru = proměnná `GOOGLE_SERVICE_ACCOUNT_JSON` (lokálně export, na GitHubu secret).

Listy vytvoří skript sám:

| List | Obsah |
|---|---|
| **Matice** | řádek = SKU, sloupce = shopy, hodnota = € bez DPH; vlevo min. cena, nejlevnější shop, Δ % vs. minulý běh a vs. 30d minimum, dostupnost |
| **Minimum** | per SKU: nejlevnější shop, cena v původní měně i v €, URL, název v shopu, poznámka ("ověřit variantu", "vyprodáno") |
| **Detail** | všechny páry SKU × shop z posledního běhu vč. stavů (nenalezeno, blokováno, chyba) |
| **Změny** | jen pohyby nad prahem / nový nejlevnější shop / nové 30d minimum |
| **Historie min** | append: datum, SKU, min. cena, shop – pro grafy vývoje |

Plná historie všech cen (každý shop, každý den) je v repu v `data/history/RRRR-MM.csv`.

## 4. Nasazení na GitHub Actions

1. Nahraj repo na GitHub (klidně **private**).
2. *Settings → Secrets and variables → Actions → New repository secret*:
   `GOOGLE_SERVICE_ACCOUNT_JSON`, `SHEET_ID`; volitelně `ANTHROPIC_API_KEY`, `PRICEBOT_PROXY`.
3. *Settings → Actions → General → Workflow permissions* → **Read and write** (Action commituje `data/`).
4. Záložka *Actions* → *price-monitor* → **Run workflow** (lze omezit na vybrané shopy a zapnout verbose log).
5. Dál běží sám každý den v 06:00 (cron v `.github/workflows/monitor.yml`; `0 4 * * *` je UTC).

Spotřeba: 65 SKU × 12 shopů ≈ 780 requestů při použití cache URL, 4 shopy paralelně s 1,5s
rozestupem → cca 6–8 min/běh (první běh s hledáním 3–4× víc). Private repo má 2 000 minut/měsíc
zdarma, to vychází s rezervou. Výstupní CSV jsou u každého běhu i jako artefakt.

## 5. Úprava master listu (`skus.csv`)

| Sloupec | Význam |
|---|---|
| `sku_id` | unikátní klíč (objeví se v Sheetu) |
| `brand`, `series`, `generation`, `category`, `name`, `unit` | popisky pro výstup |
| `part_number` | volitelné; když ho doplníš (např. ze ceníku distributora), hledá se jako první |
| `search` | výrazy pro vyhledávání v shopu, oddělené `\|`, zkouší se postupně, dokud nějaký nevrátí kandidáty s přesvědčivým názvem |
| `must_match` | regexy oddělené `;` – **všechny** musí v názvu produktu sedět (jeden regex = jedna podmínka, uvnitř může být `a\|b\|c`) |
| `must_not_match` | regexy oddělené `;` – **žádný** nesmí sedět (sady, náhradní díly, jiná generace, 1x, XPLR, 13s…) |
| `prefer` | regexy, které jen bodují: vyšší skóre vyhrává mezi kandidáty; když nesedí ani jeden, řádek dostane poznámku **"ověřit variantu"** |

Příklad nového řádku (Ultegra přehazovačka zní takto):

```
SH-ULT-RD,Shimano,Ultegra Di2,R8100,Přehazovačka,RD-R8150 Di2 12s,ks,,RD-R8150,RD[- ]?R8150,"groupset|group set|gruppe\b|…|cage|käfig|pulley|…",
```

Zásady, které se osvědčí:
* Shimano: klíč = part number (`RD[- ]?R8150`). Cena klik je stejná napříč délkami i převodníky a kazet
  napříč rozsahy, proto stačí jedna varianta na komponent; `prefer` jen hlídá, že se nechytila jiná.
* SRAM: klíč = řada + `AXS` + druh dílu ve více jazycích (EN/DE/CZ/NL/FR/ES/IT jsou v pravidlech).
  Generace se rozlišuje slovy: stará Rival/Red = `eTap`, Force D2 vs. 2025 často jen letopočtem –
  po prvním běhu projdi `data/urls.json` u SRAM Force a případně zafixuj URL ručně:
  `python -m pricebot set-url SR-FORCE-RD bike24 https://…` (odstranění: místo URL `-`).
  Doplnění SRAM part numbers do `part_number` tuhle nejistotu odstraní úplně.
* Páky: Shimano i SRAM je prodávají po stranách i v sadách – `prefer` míří na pár (Shimano) resp.
  jednu stranu vč. třmenu (SRAM), odchylku hlásí "ověřit variantu".
* Až Shimano vydá R9300 (očekávání přelom 2026/27), přidej novou řadu kopií bloku a ponech R9200
  jako "dobíhající" – přesně tam se objeví výprodeje.

## 6. Úprava shopů (`shops.yaml`)

```yaml
  - id: nase-kolo            # klíč, objeví se v data/urls.json
    name: nasekolo.cz        # hlavička sloupce v Sheetu
    country: CZ
    currency: CZK
    vat: 0.21
    search_url: "https://www.nasekolo.cz/vyhledavani/?q={q}"
    product_url_pattern: 'nasekolo\.cz/produkt/'      # volitelné – regex odkazů na produkt
    fetcher: requests                                  # nebo playwright
    accept_language: "cs-CZ,cs;q=0.9"
    enabled: true
```

Doporučení: nepřidávej desítky shopů naráz – 10–13 shopů, které reálně určují cenovou hladinu v EU,
pokryje 95 % případů a udrží běh krátký. Srovnávače (Geizhals, idealo, Heureka) záměrně nejsou –
blokují boty agresivně a ukazují dopravu jen do své země; pokud je budeš chtít, jde je přidat jako
"shop" se stejnou šablonou, ale spíš přes Playwright + proxy.

## 7. Známá omezení

* **Doprava** není započítaná (liší se shop od shopu a podle hodnoty košíku) – ceny jsou za zboží.
* **Generace SRAM Force** (D2 vs. 2025) se rozlišuje jen podle názvu – viz kap. 5.
* **Bot ochrana**: bike24, případně r2-bike, mohou z cloudových IP GitHubu vracet 403. Řešení je
  Playwright (často stačí), jinak proxy. Z GitHubu se požadavky posílají 1× denně s rozestupem –
  buď prosím ohleduplný a neposílej to častěji.
* **Ceny jsou koncové s DPH** tak, jak je shop zobrazuje; B2B/partnerské ceny nezná.
* Když shop změní strukturu stránky, spadne to nejčastěji na vyhledávání (kap. 2, `probe`), ne na
  parsování ceny – JSON-LD je stabilní.

## 8. Příkazy

```
python -m pricebot run   [--shops a,b] [--skus X,Y] [--limit N] [--no-sheet] [-v]
python -m pricebot probe [--shops a,b] [--q "RD-R8150"] [--fix] [--no-playwright]
python -m pricebot set-url SKU_ID SHOP_ID URL|-
python -m pricebot export            # přegeneruje data/*.csv z data/latest.json
python -m pytest                     # offline testy (parsování, pravidla, celý běh proti falešnému shopu)
```
