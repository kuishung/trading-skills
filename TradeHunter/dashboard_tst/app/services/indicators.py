"""Macro indicator series — the "Track" and "Read" steps of the study loop.

Phase A of MACRO_STUDY_DESIGN.md. Two jobs:
  1. FETCH a named series from its source (FRED or Yahoo) and store the raw
     observations in `MacroReading`.
  2. READ it back as a trend — latest value, change vs 3m/12m, and a sparkline.

Design notes worth keeping:

* **Raw levels are stored; transforms are applied on read.** A YoY indicator
  stores the index level, not the computed YoY. That keeps the store
  source-of-truth, lets a transform be corrected without a re-fetch, and means
  two indicators can share one underlying series.
* **Definitions live in the DB**, seeded from `SEED` below. The indicator set is
  a trading judgement, so the user must be able to change it without a deploy.
* **The board is a live view, so nothing here reads parquet.** Yahoo history
  comes through `resources/yf_daily_bars` (in-memory, no disk) — the same seam
  the scanner uses. Historical breadth computed FROM parquet is a separate,
  explicitly-permitted path (see the two-path rule in the design doc); it is not
  implemented here.
* Everything soft-fails per indicator: one dead series must not blank the board.
"""
from __future__ import annotations

import datetime as _dt

import httpx
from sqlalchemy.orm import Session

from ..config import settings
from ..models import MacroIndicator, MacroReading

_FRED_URL = "https://api.stlouisfed.org/fred/series/observations"
_UA = "TradeHunter Macro (contact: admin@tradehunter.net)"


# ---------------------------------------------------------------------------
# Seed set — PROPOSED in MACRO_STUDY_DESIGN.md, amended by the research pass
# (ACM term premium, Baker-Bloom-Davis EPU, Chicago Fed NFCI). Edit freely:
# these are only defaults for an empty table, never re-applied over user edits.
# (key, section, label, source, source_ref, unit, transform, higher_is, note)
# ---------------------------------------------------------------------------
SEED = [
    # — Monetary policy & rates —
    ("dgs2", "policy_rates", "2-year Treasury yield", "fred", "DGS2", "%", "level", "neutral",
     "Proxy for the expected policy path. CME FedWatch has no free history."),
    ("t10y2y", "policy_rates", "2s10s curve", "fred", "T10Y2Y", "%", "level", "neutral",
     "Negative = inverted. The most-studied recession lead indicator."),
    ("dfii10", "policy_rates", "10y real yield (TIPS)", "fred", "DFII10", "%", "level", "risk_off",
     None),
    ("t10yie", "policy_rates", "10y breakeven inflation", "fred", "T10YIE", "%", "level", "neutral",
     "Market-implied average inflation over 10 years."),
    # — Growth & inflation —
    ("cpi", "growth_inflation", "CPI (YoY)", "fred", "CPIAUCSL", "%", "yoy", "risk_off", None),
    ("core_pce", "growth_inflation", "Core PCE (YoY)", "fred", "PCEPILFE", "%", "yoy", "risk_off",
     "The Fed's preferred inflation gauge."),
    ("payems", "growth_inflation", "Nonfarm payrolls (MoM)", "fred", "PAYEMS", "k", "mom",
     "risk_on", None),
    ("unrate", "growth_inflation", "Unemployment rate", "fred", "UNRATE", "%", "level", "risk_off",
     None),
    ("indpro", "growth_inflation", "Industrial production (YoY)", "fred", "INDPRO", "%", "yoy",
     "risk_on", "Free substitute for ISM, whose headline series is licence-restricted."),
    # — Market internals —
    ("vix", "internals", "VIX", "yahoo", "^VIX", "", "level", "risk_off", None),
    ("hy_oas", "internals", "High-yield OAS", "fred", "BAMLH0A0HYM2", "%", "level", "risk_off",
     "Credit spreads lead equity drawdowns more reliably than equity vol."),
    ("nfci", "internals", "Chicago Fed NFCI", "fred", "NFCI", "", "level", "risk_off",
     "Financial conditions. The conditioning variable in Adrian-Boyarchenko-Giannone "
     "(Vulnerable Growth, AER 2019): tighter conditions fatten the LEFT tail of growth."),
    # — Cross-asset —
    ("dxy", "cross_asset", "Dollar index", "yahoo", "DX-Y.NYB", "", "level", "risk_off", None),
    ("tnx", "cross_asset", "10-year Treasury yield", "yahoo", "^TNX", "%", "level", "neutral",
     None),
    ("gold", "cross_asset", "Gold (GLD)", "yahoo", "GLD", "$", "level", "neutral", None),
    ("oil", "cross_asset", "Oil (USO)", "yahoo", "USO", "$", "level", "neutral", None),
    # — Global & geopolitical —
    ("epu", "global_geo", "Economic Policy Uncertainty", "fred", "USEPUINDXD", "index", "level",
     "risk_off",
     "Baker, Bloom & Davis (NBER w21633) — newspaper-derived, validated against 12,000 "
     "human-read articles. The one hard series available for this topic."),
    ("eem_spy", "global_geo", "EM vs US (EEM)", "yahoo", "EEM", "$", "level", "risk_on",
     "Read against SPY; EM leadership is a risk-appetite tell."),
    # — Liquidity & positioning —
    ("walcl", "liquidity", "Fed balance sheet", "fred", "WALCL", "$m", "level", "risk_on", None),
    ("rrp", "liquidity", "Overnight reverse repo", "fred", "RRPONTSYD", "$bn", "level", "neutral",
     None),
    ("tga", "liquidity", "Treasury General Account", "fred", "WTREGEN", "$bn", "level", "neutral",
     "A rising TGA drains reserves; net liquidity = balance sheet - RRP - TGA."),
    # — The seven Macro topics (v4.243, services/macro_topics.py). Section "topics" keeps
    #   them off the six-section study board; the topics also reuse cpi, unrate, dxy, dgs2
    #   and t10y2y from above. —
    ("pce", "topics", "PCE price index (YoY)", "fred", "PCEPI", "%", "yoy", "risk_off",
     "Headline PCE, the BEA's measure of what consumers actually pay."),
    ("ppi", "topics", "PPI, final demand (YoY)", "fred", "PPIFIS", "%", "yoy", "risk_off",
     "Producers' selling prices; more volatile than CPI or PCE."),
    ("fftr_upper", "topics", "Fed funds target, upper bound", "fred", "DFEDTARU", "%", "level",
     "neutral", "Set by the FOMC eight times a year."),
    ("fftr_lower", "topics", "Fed funds target, lower bound", "fred", "DFEDTARL", "%", "level",
     "neutral", None),
    ("effr", "topics", "Effective fed funds rate (EFFR)", "fred", "DFF", "%", "level", "neutral",
     "Where banks actually lend reserves overnight; open market operations keep it inside the "
     "target range."),
    ("dtwexbgs", "topics", "Trade-weighted dollar (broad)", "fred", "DTWEXBGS", "index", "level",
     "risk_off", "The Fed's nominal broad dollar index; weights refreshed yearly."),
    ("sahm", "topics", "Sahm rule (real time)", "fred", "SAHMREALTIME", "pp", "level", "risk_off",
     "3-month average unemployment minus its 12-month low; 0.50 or more has marked every recession "
     "start since 1970."),
    ("gdp_qoq", "topics", "Real GDP, q/q annualised", "fred", "A191RL1Q225SBEA", "%", "level",
     "risk_on", "The headline growth print, one value per quarter."),
    ("gdp_yoy", "topics", "Real GDP (YoY)", "fred", "GDPC1", "%", "yoy", "risk_on", None),
    ("gdpnow", "topics", "GDPNow (Atlanta Fed)", "fred", "GDPNOW", "%", "level", "risk_on",
     "A running estimate of the current quarter's growth, updated after each data release."),
    ("debt", "topics", "Federal debt, total public", "fred", "GFDEBTN", "$m", "level", "neutral",
     "Quarterly; stored in $ millions."),
    ("debt_gdp", "topics", "Federal debt as % of GDP", "fred", "GFDEGDQ188S", "%", "level",
     "neutral", None),
    # (v4.245 seeded spy / qqq here for the inflation charts; v4.246 removed them at the user's
    # request. A table that already holds the two rows keeps them; they are unused and harmless.)
]


def ensure_seeded(db: Session) -> int:
    """Populate the indicator definitions: every SEED entry whose key is not in the table yet
    is inserted (so a new release can add series to a live DB); existing rows are never
    overwritten, so the user's edits to a definition survive. After the first run this is one
    cheap query of the keys."""
    have = {k for (k,) in db.query(MacroIndicator.key).all()}
    added = 0
    for i, (key, section, label, source, ref, unit, transform, higher, note) in enumerate(SEED):
        if key in have:
            continue
        db.add(MacroIndicator(
            key=key, section=section, label=label, source=source, source_ref=ref,
            unit=unit, transform=transform, higher_is=higher, note=note, sort=i, active=True,
        ))
        added += 1
    if added:
        db.commit()
    return added


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------
_FRED_CSV_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv"


def parse_fred_csv(text: str) -> list[tuple[_dt.datetime, float]]:
    """The rows of FRED's public ``fredgraph.csv`` download (``observation_date,<ID>`` then one
    line per observation; a missing value is ``.``)."""
    out = []
    for line in text.splitlines()[1:]:
        parts = line.strip().split(",")
        if len(parts) < 2:
            continue
        raw = parts[1].strip()
        if raw in ("", "."):
            continue
        try:
            out.append((_dt.datetime.fromisoformat(parts[0].strip()), float(raw)))
        except ValueError:
            continue
    return out


def _fetch_fred_csv(series_id: str, start: _dt.date) -> list[tuple[_dt.datetime, float]]:
    """Observations from FRED WITHOUT an API key: the same public CSV the "Download" button on
    a FRED chart serves (v4.243, so Hermes needs no FRED key for the Macro topics)."""
    r = httpx.get(_FRED_CSV_URL, params={"id": series_id, "cosd": start.isoformat()},
                  headers={"User-Agent": _UA}, timeout=30.0, follow_redirects=True)
    r.raise_for_status()
    return parse_fred_csv(r.text)


def _fetch_fred(series_id: str, start: _dt.date) -> list[tuple[_dt.datetime, float]]:
    """Observations from FRED: the JSON API when a key is configured, else the public CSV."""
    if not settings.fred_api_key:
        return _fetch_fred_csv(series_id, start)
    r = httpx.get(_FRED_URL, params={
        "series_id": series_id, "api_key": settings.fred_api_key,
        "file_type": "json", "observation_start": start.isoformat(),
    }, headers={"User-Agent": _UA}, timeout=30.0)
    r.raise_for_status()
    out = []
    for o in (r.json().get("observations") or []):
        raw = (o.get("value") or "").strip()
        if raw in ("", "."):        # FRED encodes a missing observation as "."
            continue
        try:
            out.append((_dt.datetime.fromisoformat(o["date"]), float(raw)))
        except (ValueError, KeyError):
            continue
    return out


def _fetch_yahoo(symbol: str, start: _dt.date) -> list[tuple[_dt.datetime, float]]:
    """Daily closes from Yahoo, in memory. Never touches the parquet store —
    this is a live/operational view (see the two-path rule in the design doc)."""
    from . import resources_bridge  # noqa: F401  (puts resources.* on sys.path)
    try:
        from yf_daily_bars import fetch_daily_single  # type: ignore
    except ImportError:                      # resources/ itself not on sys.path (a one-off script)
        from resources.yf_daily_bars import fetch_daily_single  # type: ignore

    days = max(1, (_dt.date.today() - start).days)
    if "." in symbol or "=" in symbol:
        # v4.243: the batch fetcher maps "." to "-" for share classes (BRK.B), which breaks
        # an index symbol such as DX-Y.NYB (the dollar index); ask yfinance directly.
        import yfinance as yf  # type: ignore

        df = yf.Ticker(symbol).history(period=f"{days}d", interval="1d", auto_adjust=False)
        bars = []
        for idx, row in df.iterrows():
            try:
                bars.append({"t": idx.to_pydatetime().replace(tzinfo=None), "c": float(row["Close"])})
            except (KeyError, TypeError, ValueError):
                continue
    else:
        bars = fetch_daily_single(symbol, lookback_days=days) or []
    out = []
    for b in bars:
        t, c = b.get("t"), b.get("c")
        if c is None or t is None:
            continue
        if isinstance(t, str):
            try:
                t = _dt.datetime.fromisoformat(t.replace("Z", "+00:00"))
            except ValueError:
                continue
        out.append((t.replace(tzinfo=None), float(c)))
    return out


def refresh(db: Session, ind: MacroIndicator, *, years: int = 25) -> dict:
    """Fetch and store one indicator's observations.

    Idempotent: a re-run fills gaps and appends; an existing (indicator, as_of) row whose
    value the source now reports differently is UPDATED (v4.243 - GDP, payrolls and the
    price indexes are revised after their first print, and the chart must show the revised
    series). Returns a small report for the UI.
    """
    start = _dt.date.today() - _dt.timedelta(days=365 * years)
    try:
        if ind.source == "fred":
            points = _fetch_fred(ind.source_ref, start)
        elif ind.source == "yahoo":
            points = _fetch_yahoo(ind.source_ref, start)
        else:
            return {"key": ind.key, "ok": False, "error": f"source '{ind.source}' not fetchable",
                    "added": 0}
    except Exception as exc:  # noqa: BLE001
        return {"key": ind.key, "ok": False, "error": f"{type(exc).__name__}: {exc}"[:200],
                "added": 0}

    have = {
        r.as_of: r for r in
        db.query(MacroReading).filter(MacroReading.indicator_key == ind.key).all()
    }
    added = revised = 0
    for as_of, value in points:
        row = have.get(as_of)
        if row is None:
            db.add(MacroReading(indicator_key=ind.key, as_of=as_of, value=value))
            have[as_of] = True
            added += 1
        elif row is not True and abs(row.value - value) > 1e-9:
            row.value = value
            revised += 1
    db.commit()
    return {"key": ind.key, "ok": True, "added": added, "revised": revised, "total": len(points)}


def refresh_section(db: Session, section: str, *, years: int = 25) -> list[dict]:
    """Refresh every active indicator in one macro section."""
    inds = (
        db.query(MacroIndicator)
        .filter(MacroIndicator.section == section, MacroIndicator.active.is_(True))
        .order_by(MacroIndicator.sort)
        .all()
    )
    return [refresh(db, i, years=years) for i in inds]


def refresh_keys(db: Session, keys, *, years: int = 25) -> list[dict]:
    """Refresh the named indicators (the Macro topics' series; v4.243)."""
    inds = (
        db.query(MacroIndicator)
        .filter(MacroIndicator.key.in_(list(keys)), MacroIndicator.active.is_(True))
        .order_by(MacroIndicator.sort)
        .all()
    )
    return [refresh(db, i, years=years) for i in inds]


def points_for(db: Session, ind: MacroIndicator, *, limit: int | None = None
               ) -> list[tuple[_dt.datetime, float]]:
    """One indicator's series, transformed for reading (YoY, MoM or the level), oldest first.
    ``limit`` keeps only the newest N STORED rows before the transform (a YoY needs a year of
    earlier rows, so ask for more than you will show)."""
    q = (db.query(MacroReading.as_of, MacroReading.value)
         .filter(MacroReading.indicator_key == ind.key))
    if limit:
        rows = q.order_by(MacroReading.as_of.desc()).limit(limit).all()
        rows.reverse()
    else:
        rows = q.order_by(MacroReading.as_of).all()
    return _transform([(t, v) for t, v in rows], ind.transform or "level")


# ---------------------------------------------------------------------------
# Reading — the trend, never a bare snapshot
# ---------------------------------------------------------------------------
def _transform(points: list[tuple[_dt.datetime, float]], how: str
               ) -> list[tuple[_dt.datetime, float]]:
    """Apply the read-time transform. Levels are stored raw so this can change
    without re-fetching."""
    if how == "level" or len(points) < 2:
        return points
    out = []
    for i, (t, v) in enumerate(points):
        if how == "yoy":
            ref = _value_near(points[:i + 1], t - _dt.timedelta(days=365))
            if ref:
                out.append((t, (v / ref - 1.0) * 100.0))
        elif how == "mom":
            prev = points[i - 1][1] if i else None
            if prev is not None:
                out.append((t, v - prev))
    return out


def _value_near(points, target: _dt.datetime):
    """Last value at or before `target` — nearest-earlier, so a missing exact
    date (holiday, ragged macro calendar) doesn't blank the comparison."""
    best = None
    for t, v in points:
        if t <= target:
            best = v
        else:
            break
    return best


def summary(db: Session, ind: MacroIndicator, *, spark_points: int = 60) -> dict:
    """Latest reading + change vs 3m/12m + a sparkline path. `None` values mean
    'no data yet' and render as an em dash rather than a zero."""
    rows = (
        db.query(MacroReading)
        .filter(MacroReading.indicator_key == ind.key)
        .order_by(MacroReading.as_of)
        .all()
    )
    points = _transform([(r.as_of, r.value) for r in rows], ind.transform or "level")
    if not points:
        return {"ind": ind, "latest": None, "as_of": None, "chg_3m": None,
                "chg_12m": None, "spark": None, "n": 0}
    last_t, last_v = points[-1]
    v3 = _value_near(points, last_t - _dt.timedelta(days=91))
    v12 = _value_near(points, last_t - _dt.timedelta(days=365))
    return {
        "ind": ind,
        "latest": last_v,
        "as_of": last_t,
        "chg_3m": (last_v - v3) if v3 is not None else None,
        "chg_12m": (last_v - v12) if v12 is not None else None,
        "spark": _spark(points[-spark_points:]),
        "n": len(points),
    }


def _spark(points) -> str | None:
    """SVG polyline points for a 120x28 sparkline. Server-rendered: no chart
    library, no JS, and it works inside an HTMX fragment swap."""
    vals = [v for _, v in points]
    if len(vals) < 2:
        return None
    lo, hi = min(vals), max(vals)
    span = (hi - lo) or 1.0
    w, h, pad = 120.0, 28.0, 2.0
    step = w / (len(vals) - 1)
    return " ".join(
        f"{i * step:.1f},{pad + (h - 2 * pad) * (1 - (v - lo) / span):.1f}"
        for i, v in enumerate(vals)
    )


def section_summaries(db: Session, section: str) -> list[dict]:
    """Every active indicator in a section, ready for the template."""
    inds = (
        db.query(MacroIndicator)
        .filter(MacroIndicator.section == section, MacroIndicator.active.is_(True))
        .order_by(MacroIndicator.sort)
        .all()
    )
    return [summary(db, i) for i in inds]
