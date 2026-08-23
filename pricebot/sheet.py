"""Push tables to Google Sheets (service account). Skips silently when credentials are missing."""
from __future__ import annotations

import json
import os
from typing import Callable


def push(tables: dict, settings: dict, log: Callable[[str], None] = print) -> bool:
    creds = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    sheet_id = os.environ.get("SHEET_ID")
    if not creds or not sheet_id:
        log("Google Sheet: GOOGLE_SERVICE_ACCOUNT_JSON / SHEET_ID nenastaveno – zápis přeskočen.")
        return False

    import gspread  # lazy import

    gc = gspread.service_account_from_dict(json.loads(creds))
    sh = gc.open_by_key(sheet_id)
    names = settings["sheet"]

    def ws_for(title: str, rows: int, cols: int):
        try:
            ws = sh.worksheet(title)
            if ws.row_count < rows or ws.col_count < cols:
                ws.resize(rows=max(rows, ws.row_count), cols=max(cols, ws.col_count))
            return ws
        except gspread.WorksheetNotFound:
            return sh.add_worksheet(title=title, rows=rows, cols=cols)

    def replace(title: str, values: list[list]) -> None:
        ws = ws_for(title, len(values) + 20, max(len(v) for v in values) + 2)
        ws.clear()
        ws.update(values=values, range_name="A1", value_input_option="RAW")
        try:
            ws.freeze(rows=1, cols=2 if title == names["matrix"] else 1)
            ws.format("1:1", {"textFormat": {"bold": True}})
        except Exception:
            pass  # cosmetics only

    replace(names["matrix"], tables["matrix"])
    replace(names["minimum"], tables["minimum"])
    replace(names["detail"], tables["detail"])
    replace(names["changes"], tables["changes"])

    # Append-only daily minimum history
    hist = tables["min_rows"]
    header = ["Datum", "SKU", "Min € bez DPH", "Shop", "Cena v shopu", "Měna", "URL"]
    ws = ws_for(names["history"], 1000, len(header))
    if not ws.row_values(1):
        ws.update(values=[header], range_name="A1", value_input_option="RAW")
    if hist:
        ws.append_rows(
            [[r["date"], r["sku_id"], r["price_eur_net"], r["shop_id"], r["price_local"], r["currency"], r["url"]] for r in hist],
            value_input_option="RAW",
        )
    log(f"Google Sheet: zapsáno ({len(tables['matrix']) - 1} SKU, {len(tables['detail']) - 1} řádků detailu).")
    return True
