"""v4.232: the Volatility Surface and the Interpolated surface as an Excel workbook (``services/vol_export.py`` +
``/options/api/surface/xlsx`` + the "Download Excel" buttons). The user: *"I need to be able to download data from vol
surface IV, price, contract expiry, strike (no need greeks) in to excel. Both the vol surface and the Interpolated"*."""
from __future__ import annotations

import datetime as _dt
import io
from pathlib import Path

import pytest

openpyxl = pytest.importorskip("openpyxl")

from app.services import vol_cm, vol_export, vol_surface  # noqa: E402
from tests.test_options_page import client, fake, no_screener_db, screener_url  # noqa: E402,F401 - the app + a screener DB
from tests.test_vol_surface import DEC, NOV, sdb  # noqa: E402,F401 - AAPL at 101, two expiries x three strikes

HTML = (Path(__file__).resolve().parent.parent / "app" / "templates" / "options.html").read_text(encoding="utf-8")


def _sheet(blob: bytes, name: str) -> list[tuple]:
    wb = openpyxl.load_workbook(io.BytesIO(blob))
    return [tuple(r) for r in wb[name].iter_rows(values_only=True)]


def _table(rows, first: str) -> tuple[tuple, list[tuple]]:
    i = next(n for n, r in enumerate(rows) if r and r[0] == first)
    return rows[i], [r for r in rows[i + 1:] if r and r[0] is not None]


def test_the_listed_surface_workbook_has_every_contract_and_the_grid(sdb):
    g = vol_surface.surface(sdb, "AAPL", side="otm", band=0, common=False)
    blob = vol_export.workbook(g, "surface")
    wb = openpyxl.load_workbook(io.BytesIO(blob))
    assert wb.sheetnames == ["Contracts", "IV grid"]
    rows = _sheet(blob, "Contracts")
    assert rows[0][0] == "AAPL - Volatility Surface (listed expiries)"
    assert rows[1][:2] == ("Ticker", "AAPL") and rows[3][:2] == ("Stock price", 101.0)
    head, body = _table(rows, "Expiry")
    assert head == ("Expiry", "Days to expiry", "Monthly", "Strike", "Call / put", "IV %", "Price (at that IV)", "Last trade")
    assert len(body) == g["n_cells"] == 6                            # every cell, no greeks
    put90 = next(r for r in body if r[3] == 90.0 and r[0].date() == NOV)
    assert put90[4] == "Put" and put90[5] == 30.0 and put90[6] == pytest.approx(1.85) and put90[7] == 1.8
    call110 = next(r for r in body if r[3] == 110.0 and r[0].date() == DEC)
    assert call110[4] == "Call" and call110[5] == 25.0 and call110[6] is None     # the 110s carry no price
    assert not any("delta" in str(c).lower() for r in rows for c in r if c)
    grid = _sheet(blob, "IV grid")
    gh, gb = _table(grid, "Strike")
    assert gh[1].startswith(NOV.isoformat()) and gh[2].startswith(DEC.isoformat())
    assert gb == [(90.0, 30.0, 30.0), (100.0, 30.0, 30.0), (110.0, 25.0, 25.0)]
    assert vol_export.filename(g, "surface") == "tradehunter-AAPL-volsurface-2026-10-07.xlsx"


def test_the_interpolated_workbook_has_the_horizon_dates_and_how_each_iv_was_got(sdb):
    g = vol_cm.surface_cm(sdb, "AAPL", side="otm", band=0, common=False, horizons=(50, 60))
    blob = vol_export.workbook(g, "cm")
    wb = openpyxl.load_workbook(io.BytesIO(blob))
    assert wb.sheetnames[0] == "Contracts" and "IV grid (strike)" in wb.sheetnames
    head, body = _table(_sheet(blob, "Contracts"), "Horizon (days)")
    assert head == ("Horizon (days)", "Horizon date", "Strike", "Call / put", "IV %", "Price (at that IV)", "How")
    r = next(x for x in body if x[0] == 60 and x[2] == 90.0)
    assert r[1].date() == _dt.date(2026, 10, 7) + _dt.timedelta(days=60) and r[3] == "Put" and r[4] == 30.0
    assert r[6].startswith("interpolated between %s" % NOV.isoformat()) and r[5] is not None
    gh, _gb = _table(_sheet(blob, "IV grid (strike)"), "Strike")
    assert gh[1] == "50d 2026-11-26" and gh[2] == "60d 2026-12-06"
    assert vol_export.filename(g, "cm") == "tradehunter-AAPL-interpolated-2026-10-07.xlsx"


def test_the_download_api_and_the_buttons(client, fake, screener_url):
    from app import screener_db
    from app.screener_models import ScrContract, ScrUnderlying

    asof = _dt.datetime(2026, 10, 7, 19, 30)
    with screener_db.session() as s:
        s.add(ScrUnderlying(symbol="AAPL", name="Apple", sec_type="stock", spot=100.0, history_done=False, history_tries=0))
        for exp in ("2026-11-20", "2026-12-18"):
            for k in (90.0, 100.0, 110.0):
                for rt in "CP":
                    s.add(ScrContract(symbol="AAPL", expiry=exp, right=rt, strike=k, weekly=False, iv=0.3,
                                      session="2026-10-07", as_of=asof, last=2.5, price=2.6, oi=100))
        s.commit()
    r = client.get("/options/api/surface/xlsx", params={"symbol": "aapl", "tab": "surface", "band": "0", "common": "0"})
    assert r.status_code == 200 and r.headers["content-type"].startswith(
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    assert 'filename="tradehunter-AAPL-volsurface-2026-10-07.xlsx"' in r.headers["content-disposition"]
    assert len(_table(_sheet(r.content, "Contracts"), "Expiry")[1]) == 6
    r = client.get("/options/api/surface/xlsx", params={"symbol": "AAPL", "tab": "cm", "horizons": "50", "band": "0"})
    assert r.status_code == 200 and "interpolated" in r.headers["content-disposition"]
    bad = client.get("/options/api/surface/xlsx", params={"symbol": "NOPE"})
    assert bad.status_code == 400 and bad.json()["ok"] is False and "NOPE" in bad.json()["error"]
    assert client.get("/options/api/surface/xlsx", params={"symbol": "AAPL", "tab": "cm", "horizons": "x"}).status_code == 400
    # the buttons: one on each tab's info line, sending the page's own settings
    assert 'id="volXlsx" class="vol-xlsx"' in HTML and 'id="cmXlsx" class="vol-xlsx"' in HTML
    fn = HTML[HTML.index("function xlsxDownload("):HTML.index("function cmParams(")]
    assert "var q = tab === 'cm' ? cmParams(sym) : surfaceParams(sym);" in fn and "q.set('tab', tab);" in fn
    assert "'/options/api/surface/xlsx?' + q.toString()" in fn
    assert "xlsxDownload('surface', $('volXlsx'), $('volMsg'))" in HTML and "xlsxDownload('cm', $('cmXlsx'), $('cmMsg'))" in HTML
