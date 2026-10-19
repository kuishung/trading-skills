"""The shared Finviz scraper (``resources/finviz_screener.py``) against Finviz's 2026 page,
where the rows live in an embedded JSON block instead of ``data-boxover-*`` attributes
and the TS comment. Found 2026-10-19 when a saved screener loaded no tickers. No network:
a synthetic page in the new shape, and one in the old shape for the fallback."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

MOD = Path(__file__).resolve().parents[2] / "resources" / "finviz_screener.py"


@pytest.fixture(scope="module")
def fs():
    spec = importlib.util.spec_from_file_location("finviz_screener_under_test", MOD)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _page(rows, total=None, view="111"):
    props = {"url": "/screener/stocks?v=%s" % view, "data": {"rows": rows, "totalCount": total or len(rows),
                                                            "pageSize": 20, "firstRow": 1}}
    return ('<!DOCTYPE html><html><head><title>Stock Screener</title></head><body>'
            '<div data-ssr-island="Screener" id="ssr-island-1"></div>'
            '<script type="application/json" data-ssr-props="ssr-island-1">%s</script>'
            '</body></html>' % json.dumps(props))


def _row(ticker, company="Co", industry="Semiconductors", sector="Technology", **numbers):
    return {"text": {"ticker": ticker, "company": company, "sector": sector, "industry": industry,
                     "securityType": "Stock", "country": "USA", "type": "stock"},
            "numbers": numbers}


NEW = _page([
    _row("NVDA", "NVIDIA Corp", marketCap=5160533.1, price=214.13, change=-1.427, volume=121252522,
         perf1w=-6.6, perf4w=-2.37, perf13w=5.33, perf26w=6.17, perf52w=17.77, perfYtd=14.81),
    _row("BRK-B", "Berkshire Hathaway", industry="Insurance", sector="Financial", price=480.5, volume=3_000_000),
    _row("NVDA", "dup row"),                      # duplicates are dropped
    {"text": {"company": "no ticker"}, "numbers": {}},
], total=25)

OLD = ('<html><body><table><tr><td><a data-boxover-ticker="AAPL" data-boxover-company="Apple" '
       'data-boxover-industry="Consumer Electronics">AAPL</a></td><td data-boxover-ticker="AAPL"></td></tr>'
       '<tr><td><a data-boxover-ticker="MSFT" data-boxover-company="Microsoft" data-boxover-industry="Software">M</a></td></tr>'
       '</table><!-- TS\nAAPL|250.10|1000\nMSFT|500.25|2000\n--></body></html>')


def test_ssr_rows_reads_the_json(fs):
    rows, total = fs._ssr_rows(NEW)
    assert total == 25 and [r["symbol"] for r in rows] == ["NVDA", "BRK.B"]       # dotted share class, deduped
    n = rows[0]
    assert n["company"] == "NVIDIA Corp" and n["industry"] == "Semiconductors" and n["sector"] == "Technology"
    assert n["price"] == 214.13 and n["volume"] == 121252522 and n["mcap"] == pytest.approx(5.1605331e12)
    assert n["chg_1d"] == -1.427 and n["perf_1w"] == -6.6 and n["perf_1m"] == -2.37 and n["perf_1y"] == 17.77
    b = rows[1]
    assert b["mcap"] is None and b["perf_1w"] is None and b["volume"] == 3_000_000


def test_extractors_prefer_the_json(fs):
    assert fs._extract_symbols(NEW) == ["NVDA", "BRK.B"]
    assert fs._extract_rows(NEW) == [{"symbol": "NVDA", "price": 214.13, "volume": 121252522},
                                     {"symbol": "BRK.B", "price": 480.5, "volume": 3_000_000}]
    perf = fs._extract_perf_rows(NEW)
    assert perf[0]["symbol"] == "NVDA" and perf[0]["perf_ytd"] == 14.81 and perf[0]["mcap"] == pytest.approx(5.1605331e12)
    assert set(perf[0]) == {"symbol", "company", "mcap", "price", "chg_1d", "perf_1w", "perf_1m", "perf_3m",
                            "perf_6m", "perf_ytd", "perf_1y"}


def test_old_markup_still_works(fs):
    assert fs._ssr_rows(OLD) == ([], None)
    assert fs._extract_symbols(OLD) == ["AAPL", "MSFT"]
    assert fs._extract_rows(OLD) == [{"symbol": "AAPL", "price": 250.10, "volume": 1000},
                                     {"symbol": "MSFT", "price": 500.25, "volume": 2000}]


def test_a_page_without_data_gives_nothing(fs):
    assert fs._extract_symbols("<html><body>Access denied</body></html>") == []
    assert fs._ssr_rows('<script type="application/json" data-ssr-props="x">not json</script>') == ([], None)


def test_walkers_use_the_json_and_stop_at_the_total(fs, monkeypatch, tmp_path):
    """One page says totalCount = 2 and holds 2 rows: the walkers must not fetch a second page."""
    page = _page([_row("NVDA", price=1.0, volume=1), _row("AMD", price=2.0, volume=2)], total=2)
    calls = []

    def fake_fetch(url):
        calls.append(url)
        return page
    monkeypatch.setattr(fs, "_fetch_page", fake_fetch)
    monkeypatch.setattr(fs, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(fs, "_PAGE_SLEEP_S", 0.0)
    url = "https://finviz.com/screener.ashx?v=111&t=NVDA,AMD"
    assert fs.fetch_screener_symbols(url, force_refresh=True) == ["NVDA", "AMD"]
    assert len(calls) == 1
    rows = fs.fetch_screener_rows(url, force_refresh=True)
    assert [r["symbol"] for r in rows] == ["NVDA", "AMD"] and rows[1]["price"] == 2.0
    ind = fs.fetch_ticker_industries(url, force_refresh=True, page_sleep_s=0.0)
    assert [(r["symbol"], r["industry"], r["price"]) for r in ind] == [("NVDA", "Semiconductors", 1.0), ("AMD", "Semiconductors", 2.0)]
    perf = fs.fetch_performance_rows(url, force_refresh=True, page_sleep_s=0.0)
    assert [r["symbol"] for r in perf] == ["NVDA", "AMD"]
    assert len(calls) == 4            # one fetch per walker, no looped-back second page
