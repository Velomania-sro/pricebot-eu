"""Google Sheet push: resilient credential parsing and safe skipping (no network / gspread)."""
import json

from pricebot import sheet

VALID = {"type": "service_account", "project_id": "x", "private_key": "-----BEGIN...-----"}
BOM = "\ufeff"


def test_parse_creds_strips_bom_and_whitespace():
    raw = BOM + "  " + json.dumps(VALID) + "  \n\t"     # leading BOM + surrounding whitespace
    logs = []
    assert sheet._parse_creds(raw, logs.append) == VALID
    assert logs == []                       # clean input -> no warning


def test_parse_creds_invalid_json_returns_none_with_message():
    logs = []
    assert sheet._parse_creds("{not: valid json,", logs.append) is None
    assert any("není platný JSON" in m for m in logs)   # readable message, no traceback


def test_parse_creds_non_object_returns_none():
    logs = []
    assert sheet._parse_creds('"just a string"', logs.append) is None
    assert any("není objekt JSON" in m for m in logs)


def test_push_skips_when_env_missing(monkeypatch):
    monkeypatch.delenv("GOOGLE_SERVICE_ACCOUNT_JSON", raising=False)
    monkeypatch.delenv("SHEET_ID", raising=False)
    logs = []
    assert sheet.push({}, {}, logs.append) is False
    assert any("nenastaveno" in m for m in logs)


def test_push_returns_false_on_invalid_json(monkeypatch):
    # Invalid JSON must be caught before gspread is imported/used -> no traceback, run continues.
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON", BOM + " not-a-json-blob")
    monkeypatch.setenv("SHEET_ID", "sheet-abc")
    logs = []
    assert sheet.push({}, {}, logs.append) is False
    assert any("není platný JSON" in m for m in logs)
