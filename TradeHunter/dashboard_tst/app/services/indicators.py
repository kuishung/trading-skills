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
]


def ensure_seeded(db: Session) -> int:
    """Populate the indicator definitions once, on an empty table. Never
    overwrites: after first run this is a single cheap COUNT."""
    if db.query(MacroIndicator.id).first() is not None:
        return 0
    for i, (key, section, label, source, ref, unit, transform, higher, note) in enumerate(SEED):
        db.add(MacroIndicator(
            key=key, section=section, label=label, source=source, source_ref=ref,
            unit=unit, transform=transform, higher_is=higher, note=note, sort=i, active=True,
        ))
    db.commit()
    return len(SEED)


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------
def _fetch_fred(series_id: str, start: _dt.date) -> list[tuple[_dt.datetime, float]]:
    """Observations from FRED. Returns [] when no key is configured (the caller
    reports 'not configured' rather than failing)."""
    if not settings.fred_api_key:
        return []
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
    from yf_daily_bars import fetch_daily_single  # type: ignore

    days = max(1, (_dt.date.today() - start).days)
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

    Idempotent: existing (indicator, as_of) rows are left alone, so a re-run only
    fills gaps and appends. Returns a small report for the UI.
    """
    start = _dt.date.today() - _dt.timedelta(days=365 * years)
    try:
        if ind.source == "fred":
            if not settings.fred_api_key:
                return {"key": ind.key, "ok": False, "error": "FRED key not configured", "added": 0}
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
        r.as_of for r in
        db.query(MacroReading.as_of).filter(MacroReading.indicator_key == ind.key).all()
    }
    added = 0
    for as_of, value in points:
        if as_of in have:
            continue
        db.add(MacroReading(indicator_key=ind.key, as_of=as_of, value=value))
        have.add(as_of)
        added += 1
    db.commit()
    return {"key": ind.key, "ok": True, "added": added, "total": len(points)}


def refresh_section(db: Session, section: str, *, years: int = 25) -> list[dict]:
    """Refresh every active indicator in one macro section."""
    inds = (
        db.query(MacroIndicator)
        .filter(MacroIndicator.section == section, MacroIndicator.active.is_(True))
        .order_by(MacroIndicator.sort)
        .all()
    )
    return [refresh(db, i, years=years) for i in inds]


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
