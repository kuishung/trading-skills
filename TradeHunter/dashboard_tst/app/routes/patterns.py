"""Pattern Trainer — teach a chart pattern by example on a real (parquet-backed)
chart, then calibrate + run a geometric detector and scan the universe for it.

Architecture (see PATTERN_TRAINER_DESIGN.md):
  - Chart data is loaded FROM PARQUET (daily/3min/5min) via resources.bars_store.
    This is an OFFLINE training tool, so parquet is the correct source (see the
    CARVE-OUT in CLAUDE.md's parquet scope rule).
  - Labels are DRAWN, not typed: you draw the triangle on the region that shows
    the pattern; saved positives/negatives feed the D4 calibration loop. (The old
    free-text "teaching chat" was removed once drawing became the canonical label.)
  - "Find pattern" (D5) runs the implemented geometric detector
    (strategy/patterns/<slug>/detect.py) over the chart and overlays its matches.
"""
from __future__ import annotations

import datetime as _dt
import json as _json
import re
import sys
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import (
    PT_ARCHIVED,
    PT_LEARNING,
    PT_READY,
    Pattern,
    PatternExample,
    User,
)
from ..security import require_user

# Make the sibling resources/ package importable (parquet bars store live there).
_TRADEHUNTER_ROOT = Path(__file__).resolve().parents[3]
if str(_TRADEHUNTER_ROOT) not in sys.path:
    sys.path.insert(0, str(_TRADEHUNTER_ROOT))
try:
    from resources import bars_store  # noqa: E402
except Exception:  # pragma: no cover - surfaced at request time
    bars_store = None
try:
    # the implemented geometric detector MODULE (D1-D4). Use import_module: the
    # package __init__ re-exports the detect() FUNCTION, which shadows the
    # submodule attribute, so `import ...detect as x` would bind the function.
    # import_module returns the real module (with .detect / .__version__).
    import importlib as _importlib  # noqa: E402
    _at_detect = _importlib.import_module("strategy.patterns.ascending_triangle.detect")
except Exception:  # pragma: no cover
    _at_detect = None


def _detector_for(slug: str):
    """The committed detector for a pattern slug, falling back to the
    ascending-triangle detector (the one currently implemented) so visual
    evaluation works for any pattern while others are still being built."""
    import importlib
    if slug:
        try:
            return importlib.import_module(
                f"strategy.patterns.{slug.replace('-', '_')}.detect")
        except Exception:
            pass
    return _at_detect


try:
    # The D2/D4 calibration engine — features, threshold fitter, validation gate.
    # Lazy/guarded so the page still renders if the strategy package is missing.
    from strategy.patterns import _features as _pat_features  # noqa: E402
    from strategy.patterns import _calibrate as _pat_calibrate  # noqa: E402
    from strategy.patterns import _validate as _pat_validate  # noqa: E402
    from strategy.patterns import _geometry as _pat_geo  # noqa: E402
except Exception:  # pragma: no cover
    _pat_features = _pat_calibrate = _pat_validate = _pat_geo = None

# Gating constants for the Teach -> Calibrate -> Promote loop.
_CALIB_MIN_POS = 3      # need a few confirmed positives to fit a boundary
_CALIB_MIN_NEG = 2      # ...and a few rejected near-misses (both classes required)
_PROMOTE_PASS = 0.80    # validation-suite pass-rate required before going live
_CALIB_MIN_BARS = 10    # an example window below this can't be featurised/fired

router = APIRouter(prefix="/patterns", tags=["patterns"])
templates = Jinja2Templates(
    directory=str(Path(__file__).resolve().parent.parent / "templates")
)

# Offered timeframes = the ones actually seeded in the store at ~1500 symbols.
# (daily + 1min are in the store too but 1min is only ~58 symbols; re-add either
# here if needed — _WINDOW_DAYS/_MAX_BARS already carry sane defaults for them.)
_TIMEFRAMES = ("daily", "3min", "5min")
# default lookback window per timeframe (keeps the payload bounded)
_WINDOW_DAYS = {"daily": 1825, "5min": 60, "3min": 60, "1min": 10}
# cap the default-window bar count per timeframe (bounds the JSON payload)
_MAX_BARS = {"daily": 2500, "5min": 4000, "3min": 5000, "1min": 3000}


def _slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return s or "pattern"


def _unique_slug(db: Session, base: str) -> str:
    slug, i = base, 2
    while db.query(Pattern).filter(Pattern.slug == slug).first() is not None:
        slug = f"{base}-{i}"
        i += 1
    return slug


def _get_pattern(db: Session, pattern_id: int, user: User) -> Pattern:
    p = db.get(Pattern, pattern_id)
    if p is None:
        raise HTTPException(status_code=404, detail="Pattern not found")
    if p.owner_id != user.id and not user.can_moderate:
        raise HTTPException(status_code=403, detail="Not your pattern")
    return p


def _to_lwc_time(t_iso: str, tf: str):
    """Convert a bars_store ISO timestamp to a Lightweight-Charts time value:
    'YYYY-MM-DD' for daily, epoch seconds (UTC) for intraday."""
    if tf == "daily":
        return t_iso[:10]
    dt = _dt.datetime.fromisoformat(t_iso.replace("Z", "+00:00"))
    return int(dt.timestamp())


def _load_window(symbol: str, tf: str, start: str | None, end: str | None) -> list[dict]:
    """Load bars for symbol@tf. With explicit start+end (a marked region) load
    exactly that range. Otherwise load the recent default window for that
    timeframe (bounded by days + a bar-count cap).

    NB: we deliberately do NOT use `available_range_fast` to find the window —
    it returns None for parquet files written without `t` column statistics
    (the intraday seeds), which would wrongly yield "no bars". We read the file
    and slice from the actual last bar instead, so it works regardless."""
    if bars_store is None:
        return []
    symbol = symbol.upper().strip()
    if not symbol:
        return []
    if start and end:
        return bars_store.load_bars(symbol, start=start, end=end, timeframe=tf)

    bars = bars_store.load_bars(symbol, timeframe=tf)
    if not bars:
        return []
    last_iso = bars[-1]["t"]
    try:
        last_dt = _dt.datetime.fromisoformat(last_iso.replace("Z", "+00:00"))
        cutoff = (last_dt - _dt.timedelta(days=_WINDOW_DAYS.get(tf, 60))).isoformat()
        bars = [b for b in bars if b["t"] >= cutoff]
    except Exception:
        pass
    return bars[-_MAX_BARS.get(tf, 5000):]


def _ema(values: list[float], period: int) -> list:
    """EMA seeded with the SMA of the first `period` values (None before that) —
    matches the chart's client-side EMA exactly. Pure."""
    out: list = [None] * len(values)
    if period <= 0 or len(values) < period:
        return out
    k = 2.0 / (period + 1)
    prev = sum(values[:period]) / period
    out[period - 1] = prev
    for i in range(period, len(values)):
        prev = values[i] * k + prev * (1.0 - k)
        out[i] = prev
    return out


def _ema_periods(tf: str) -> tuple[int, int]:
    """(mid, slow) EMA periods for the trend gate — the same EMAs drawn on the
    chart (daily 50/200, intraday 18/50)."""
    return (50, 200) if tf == "daily" else (18, 50)


def _filter_uptrend(bars: list[dict], matches: list[dict], tf: str) -> list[dict]:
    """Keep only matches whose window ends in an UPTREND, judged by the EMAs drawn
    on the chart: last close above the slow EMA, mid EMA above the slow EMA, and the
    slow EMA rising. An ascending triangle is a bullish continuation, so this drops
    the geometrically-similar shapes that form in downtrends/ranges. Falls back to
    the mid EMA when there isn't enough history to seed the slow EMA (keeps the match
    if neither EMA is available)."""
    if not matches:
        return matches
    closes = [float(b["c"]) for b in bars]
    idx = {b["t"]: i for i, b in enumerate(bars)}
    mid_p, slow_p = _ema_periods(tf)
    ema_mid, ema_slow = _ema(closes, mid_p), _ema(closes, slow_p)
    look = 10
    kept = []
    for m in matches:
        i = idx.get(m["end_t"])
        if i is None:
            kept.append(m)
            continue
        c, mid, slow = closes[i], ema_mid[i], ema_slow[i]
        if slow is not None:
            rising = i >= look and ema_slow[i - look] is not None and slow > ema_slow[i - look]
            up = (c > slow) and (mid is None or mid > slow) and rising
        elif mid is not None:
            up = c > mid
        else:
            up = True   # not enough history to judge — don't filter it out
        if up:
            kept.append(m)
    return kept


try:
    from zoneinfo import ZoneInfo
    _ET_TZ = ZoneInfo("America/New_York")
except Exception:  # pragma: no cover
    _ET_TZ = None


def _is_rth(iso: str) -> bool:
    """True if the bar's Eastern time is within regular trading hours
    (09:30–16:00 ET). DST handled by zoneinfo. Falls back to True if zoneinfo is
    unavailable so we never silently drop everything."""
    if _ET_TZ is None:
        return True
    try:
        dt = _dt.datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(_ET_TZ)
    except Exception:  # noqa: BLE001
        return True
    m = dt.hour * 60 + dt.minute
    return 570 <= m < 960


def _rth_bars(bars: list[dict], tf: str) -> list[dict]:
    """Detection runs on REGULAR-HOURS bars only (intraday): extended hours are for
    visual session separation, not pattern detection. Daily is RTH by nature."""
    if tf == "daily":
        return bars
    return [b for b in bars if _is_rth(b["t"])]


def _filter_valid_triangle(bars: list[dict], matches: list[dict]) -> list[dict]:
    """Drop matches that aren't valid ascending triangles: if price CLOSES below the
    rising support line anywhere inside the window (beyond a small tolerance), the
    support was breached — a breakdown, not a triangle that breaks resistance UP.
    The support line is the fitted {support:{t0,p0,t1,p1}} carried on each match."""
    idx = {b["t"]: i for i, b in enumerate(bars)}
    out = []
    for m in matches:
        sup = (m.get("lines") or {}).get("support")
        i0, i1 = idx.get(m["start_t"]), idx.get(m["end_t"])
        if not sup or i0 is None or i1 is None or i1 <= i0:
            out.append(m)
            continue
        p0, p1, span = sup["p0"], sup["p1"], (i1 - i0)
        breached = False
        for i in range(i0, i1 + 1):
            sv = p0 + (p1 - p0) * ((i - i0) / span)
            if bars[i]["c"] < sv * 0.998:      # 0.2% below the rising support = breach
                breached = True
                break
        if not breached:
            out.append(m)
    return out


def _filter_min_height(bars: list[dict], matches: list[dict], min_atr: float) -> list[dict]:
    """Drop matches whose triangle HEIGHT (resistance ceiling − lowest low in the
    window) is smaller than `min_atr` × ATR — tiny/flat patterns are noise. ATR is
    measured on the match window so the threshold is ticker-relative."""
    if min_atr <= 0 or _pat_geo is None:
        return matches
    idx = {b["t"]: i for i, b in enumerate(bars)}
    out = []
    for m in matches:
        i0, i1 = idx.get(m["start_t"]), idx.get(m["end_t"])
        if i0 is None or i1 is None or i1 <= i0:
            out.append(m)
            continue
        window = bars[i0:i1 + 1]
        atr = _pat_geo.atr(window)
        res = (m.get("lines") or {}).get("resistance")
        ceiling = max(res["p0"], res["p1"]) if res else max(b["h"] for b in window)
        low = min(b["l"] for b in window)
        if atr > 0 and (ceiling - low) / atr >= min_atr:
            out.append(m)
    return out


def _apply_rules(raw, matches, tf, *, valid, trend, min_height_atr):
    """Apply the user-toggled detection rules in order, returning the kept matches.
    Each is independently engaged from the rules panel; order is cheap→strong."""
    if valid:
        matches = _filter_valid_triangle(raw, matches)
    if trend == "up":
        matches = _filter_uptrend(raw, matches, tf)
    if min_height_atr and min_height_atr > 0:
        matches = _filter_min_height(raw, matches, min_height_atr)
    return matches


def _example_counts(p: Pattern) -> tuple[int, int]:
    pos = sum(1 for e in p.examples if (e.kind or "positive") != "negative")
    neg = sum(1 for e in p.examples if (e.kind or "positive") == "negative")
    return pos, neg


def _readiness(p: Pattern) -> dict:
    """The pattern's position in the Teach -> Calibrate -> Test -> Promote loop,
    plus the single next action. Drives the trainer's readiness card + the gating
    of the Calibrate / Promote / Scan buttons."""
    pos, neg = _example_counts(p)
    calibrated = bool(p.detector_thresholds)
    pass_rate = p.calib_pass_rate
    ready = p.status == PT_READY
    can_calibrate = pos >= _CALIB_MIN_POS and neg >= _CALIB_MIN_NEG
    passing = pass_rate is not None and pass_rate >= _PROMOTE_PASS
    can_promote = calibrated and passing and not ready

    if ready:
        hint = "Live — promoted; run it across the universe."
    elif not can_calibrate:
        need = []
        if pos < _CALIB_MIN_POS:
            need.append(f"{_CALIB_MIN_POS - pos} more positive")
        if neg < _CALIB_MIN_NEG:
            need.append(f"{_CALIB_MIN_NEG - neg} more counter")
        hint = "Teach " + " and ".join(need) + " example(s) to unlock calibration."
    elif not calibrated:
        hint = "Calibrate the detector from your examples."
    elif not passing:
        hint = (f"Suite at {round((pass_rate or 0) * 100)}% — correct the misfires "
                f"and recalibrate to reach {round(_PROMOTE_PASS * 100)}%.")
    else:
        hint = "Calibrated and passing — promote to scan the universe."

    return {
        "pos": pos, "neg": neg, "calibrated": calibrated,
        "pass_rate": pass_rate, "can_calibrate": can_calibrate,
        "passing": passing, "can_promote": can_promote, "ready": ready,
        "status": p.status, "min_pos": _CALIB_MIN_POS, "min_neg": _CALIB_MIN_NEG,
        "promote_pass": _PROMOTE_PASS, "hint": hint,
        "version": p.detector_version,
        "calib_at": p.calib_at.isoformat() if p.calib_at else None,
        "engine_ok": all((_pat_features, _pat_calibrate, _pat_validate)),
    }


def _build_labeled(p: Pattern):
    """Turn the saved examples into (labeled feature set, suite cases) for the
    calibrator + validator. Each example's window is re-loaded from parquet and
    featurised; positives -> label 1, counters -> label 0. Windows too short to
    featurise are skipped. Returns (labeled, cases, skipped)."""
    labeled: list[dict] = []
    cases: dict = {"positives": [], "negatives": []}
    skipped = 0
    for e in p.examples:
        bars = _load_window(e.symbol, e.timeframe, e.start_t, e.end_t)
        if len(bars) < _CALIB_MIN_BARS:
            skipped += 1
            continue
        feats = _pat_features.window_features(bars)
        label = 0 if (e.kind or "positive") == "negative" else 1
        labeled.append({"features": feats, "label": label,
                        "symbol": e.symbol, "t": e.start_t})
        bucket = "negatives" if label == 0 else "positives"
        cases[bucket].append({"bars": bars, "id": f"{e.symbol}:{e.start_t}"})
    return labeled, cases, skipped


# --------------------------------------------------------------------------- #
# Pages
# --------------------------------------------------------------------------- #
@router.get("", response_class=HTMLResponse)
def patterns_home(request: Request, user: User = Depends(require_user),
                  db: Session = Depends(get_db)):
    q = db.query(Pattern).filter(Pattern.status != PT_ARCHIVED)
    if not user.can_moderate:
        q = q.filter(Pattern.owner_id == user.id)
    patterns = q.order_by(Pattern.updated_at.desc()).all()
    return templates.TemplateResponse(
        request, "patterns.html",
        {"user": user, "patterns": patterns,
         "bars_ok": bars_store is not None},
    )


@router.post("")
def create_pattern(request: Request, name: str = Form(...),
                   description: str = Form(""),
                   user: User = Depends(require_user), db: Session = Depends(get_db)):
    name = name.strip()
    if not name:
        return RedirectResponse(url="/patterns", status_code=303)
    slug = _unique_slug(db, _slugify(name))
    p = Pattern(owner_id=user.id, name=name[:120], slug=slug,
                description=(description.strip()[:400] or None), status=PT_LEARNING)
    db.add(p)
    db.commit()
    return RedirectResponse(url=f"/patterns/{p.id}", status_code=303)


@router.get("/{pattern_id}", response_class=HTMLResponse)
def pattern_detail(pattern_id: int, request: Request,
                   user: User = Depends(require_user), db: Session = Depends(get_db)):
    p = _get_pattern(db, pattern_id, user)
    det = _detector_for(p.slug)
    return templates.TemplateResponse(
        request, "pattern_detail.html",
        {"user": user, "p": p, "examples": p.examples,
         "timeframes": _TIMEFRAMES,
         "bars_ok": bars_store is not None,
         "readiness": _readiness(p),
         "detector": {
             "version": getattr(det, "__version__", None) if det else None,
             "thresholds": p.detector_thresholds or {},
         }},
    )


# --------------------------------------------------------------------------- #
# Chart data (parquet)
# --------------------------------------------------------------------------- #
@router.get("/{pattern_id}/bars")
def pattern_bars(pattern_id: int,
                 symbol: str = Query(...),
                 tf: str = Query("daily"),
                 start: str | None = Query(None),
                 end: str | None = Query(None),
                 user: User = Depends(require_user), db: Session = Depends(get_db)):
    p = _get_pattern(db, pattern_id, user)
    if tf not in _TIMEFRAMES:
        tf = _TIMEFRAMES[0]
    if bars_store is None:
        return JSONResponse({"ok": False, "error": "bars store unavailable", "bars": []},
                            status_code=503)
    raw = _load_window(symbol, tf, start, end)
    bars = [{"time": _to_lwc_time(b["t"], tf), "t": b["t"],
             "open": b["o"], "high": b["h"], "low": b["l"], "close": b["c"],
             "volume": b["v"]} for b in raw]

    # remember where the user was teaching, so the page reopens here
    p.chart_symbol = symbol.upper().strip()[:20]
    p.chart_timeframe = tf
    db.commit()

    # never cache price data — a stale empty response otherwise sticks in the
    # browser cache and the chart shows "no bars" even after the store is fixed.
    return JSONResponse(
        {"ok": True, "symbol": symbol.upper().strip(), "tf": tf,
         "count": len(bars), "bars": bars},
        headers={"Cache-Control": "no-store"},
    )


# --------------------------------------------------------------------------- #
# Detector overlay (D5 — visual evaluation of the geometric detector)
# --------------------------------------------------------------------------- #
@router.get("/{pattern_id}/detect")
def pattern_detect(pattern_id: int,
                   symbol: str = Query(...),
                   tf: str = Query("daily"),
                   # ---- detection rules (each toggled from the rules panel) ----
                   rth: int = Query(1),               # 1 = detect on regular-hours bars only
                   trend: str = Query("up"),          # "up" = uptrend-only (EMA gate); "any" = off
                   valid: int = Query(1),             # 1 = drop support-breach breakdowns
                   min_height_atr: float = Query(0.0),  # >0 = require height >= N x ATR
                   score_min: float = Query(-1.0),    # >=0 = override the detector's fire cutoff
                   user: User = Depends(require_user), db: Session = Depends(get_db)):
    """Run the pattern's geometric detector over the chart's window and return its
    matches (with Lightweight-Charts time values) for the overlay. The detection
    rules are engaged/adjusted from the rules panel and passed here as query params.
    Read-only / offline; never a live signal."""
    p = _get_pattern(db, pattern_id, user)
    if tf not in _TIMEFRAMES:
        tf = _TIMEFRAMES[0]
    if bars_store is None:
        return JSONResponse({"ok": False, "error": "bars store unavailable", "matches": []},
                            status_code=503)
    det = _detector_for(p.slug)
    if det is None:
        return JSONResponse({"ok": False, "error": "detector unavailable", "matches": []},
                            status_code=503)
    full = _load_window(symbol, tf, None, None)
    raw = _rth_bars(full, tf) if rth else full          # RTH-only rule
    if not raw:
        return JSONResponse({"ok": True, "symbol": symbol.upper().strip(), "tf": tf,
                             "count": 0, "matches": []}, headers={"Cache-Control": "no-store"})
    thr = dict(p.detector_thresholds or {})
    if score_min is not None and score_min >= 0:        # min-score rule (override fire cutoff)
        thr["score_min"] = float(score_min)
    try:
        matches = det.detect(raw, thresholds=thr) if thr else det.detect(raw)
    except TypeError:
        matches = det.detect(raw)            # detector without a thresholds kwarg
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": repr(exc), "matches": []},
                            status_code=500)
    n_raw = len(matches)
    matches = _apply_rules(raw, matches, tf, valid=bool(valid), trend=trend,
                           min_height_atr=min_height_atr)
    out = [{**m,
            "start_time": _to_lwc_time(m["start_t"], tf),
            "end_time": _to_lwc_time(m["end_t"], tf)} for m in matches]
    _parts = (getattr(det, "__name__", "") or "").split(".")
    detector_name = _parts[-2] if len(_parts) >= 2 else (_parts[0] if _parts else "?")
    return JSONResponse(
        {"ok": True, "symbol": symbol.upper().strip(), "tf": tf,
         "detector": detector_name, "version": getattr(det, "__version__", "?"),
         "trend": trend, "n_raw": n_raw,
         "count": len(out), "matches": out},
        headers={"Cache-Control": "no-store"},
    )


# --------------------------------------------------------------------------- #
# Saved examples (the per-pattern teaching gallery)
# --------------------------------------------------------------------------- #
def _example_json(e: PatternExample) -> dict:
    return {"id": e.id, "symbol": e.symbol, "timeframe": e.timeframe,
            "start": e.start_t, "end": e.end_t, "n": e.n_bars,
            "kind": e.kind or "positive", "geometry": e.geometry,
            "label": e.label or "", "note": e.note or ""}


def _parse_geometry(raw: str) -> dict | None:
    """Validate the drawn-trendline geometry JSON from the chart. Shape:
    {"resistance": {t0,p0,t1,p1}, "support": {t0,p0,t1,p1}} with numeric prices
    and ISO/string times. Returns the cleaned dict or None (no/!valid geometry)."""
    if not raw:
        return None
    try:
        g = _json.loads(raw)
    except (ValueError, TypeError):
        return None
    out = {}
    for side in ("resistance", "support"):
        s = g.get(side) or {}
        try:
            out[side] = {"t0": str(s["t0"]), "p0": float(s["p0"]),
                         "t1": str(s["t1"]), "p1": float(s["p1"])}
        except (KeyError, TypeError, ValueError):
            return None
    return out


@router.post("/{pattern_id}/examples.json")
def save_example(pattern_id: int,
                 symbol: str = Form(...), tf: str = Form(...),
                 start: str = Form(...), end: str = Form(...),
                 kind: str = Form("positive"), geometry: str = Form(""),
                 label: str = Form(""), note: str = Form(""),
                 user: User = Depends(require_user), db: Session = Depends(get_db)):
    """Save a drawn example. The user DRAWS the resistance + support lines on the
    chart (drag, never type); `geometry` carries those trendline endpoints and the
    numeric features are derived from it later by the calibrator. `kind` is the
    label polarity (positive | negative). Validates the region resolves to >=1
    parquet bar so we never persist an empty range."""
    p = _get_pattern(db, pattern_id, user)
    sym = symbol.upper().strip()[:20]
    if tf not in _TIMEFRAMES:
        return JSONResponse({"ok": False, "error": "bad timeframe"}, status_code=400)
    a, b = (start, end) if start <= end else (end, start)
    bars = _load_window(sym, tf, a, b)
    if not bars:
        return JSONResponse(
            {"ok": False, "error": "no bars in that range — re-draw the pattern"},
            status_code=400,
        )
    knd = "negative" if (kind or "").strip().lower() == "negative" else "positive"
    e = PatternExample(pattern_id=p.id, symbol=sym, timeframe=tf,
                       start_t=a, end_t=b, n_bars=len(bars),
                       kind=knd, geometry=_parse_geometry(geometry),
                       label=(label.strip()[:120] or None),
                       note=(note.strip()[:2000] or None))
    db.add(e)
    db.commit()
    return JSONResponse({"ok": True, "example": _example_json(e)})


@router.post("/{pattern_id}/examples/{example_id}/delete")
def delete_example(pattern_id: int, example_id: int,
                   user: User = Depends(require_user), db: Session = Depends(get_db)):
    p = _get_pattern(db, pattern_id, user)
    e = db.get(PatternExample, example_id)
    if e is not None and e.pattern_id == p.id:
        db.delete(e)
        db.commit()
    return JSONResponse({"ok": True})


@router.post("/{pattern_id}/archive")
def archive_pattern(pattern_id: int, user: User = Depends(require_user),
                    db: Session = Depends(get_db)):
    p = _get_pattern(db, pattern_id, user)
    p.status = PT_ARCHIVED
    db.commit()
    return RedirectResponse(url="/patterns", status_code=303)


# --------------------------------------------------------------------------- #
# Calibrate / Promote — the Teach -> Calibrate -> Test -> Promote loop
# --------------------------------------------------------------------------- #
@router.post("/{pattern_id}/calibrate")
def calibrate_pattern(pattern_id: int, user: User = Depends(require_user),
                      db: Session = Depends(get_db)):
    """Fit the detector's thresholds to this pattern's drawn examples (D4): derive
    per-feature cutoffs + score weights from the positive/counter clouds, pick a
    fire cutoff, then run the validation suite (positives must fire, counters must
    stay quiet) and store the pass-rate. Returns the new readiness + suite so the
    page can update without a reload. Idempotent — rerun after adding examples."""
    p = _get_pattern(db, pattern_id, user)
    det = _detector_for(p.slug)
    if not (det and _pat_features and _pat_calibrate and _pat_validate):
        return JSONResponse({"ok": False, "error": "calibration engine unavailable"},
                            status_code=503)
    pos, neg = _example_counts(p)
    if pos < _CALIB_MIN_POS or neg < _CALIB_MIN_NEG:
        return JSONResponse(
            {"ok": False, "error": f"need >={_CALIB_MIN_POS} positive and "
             f">={_CALIB_MIN_NEG} counter-examples to calibrate"},
            status_code=400)

    labeled, cases, skipped = _build_labeled(p)
    n_pos = sum(1 for r in labeled if r["label"] == 1)
    n_neg = sum(1 for r in labeled if r["label"] == 0)
    if n_pos < 1 or n_neg < 1:
        return JSONResponse(
            {"ok": False, "error": "examples resolved to too few bars — re-draw "
             "wider regions"}, status_code=400)

    thr = _pat_calibrate.fit_thresholds(labeled)
    try:
        thr["score_min"] = round(
            _pat_calibrate.fit_score_cutoff(labeled, det, target="balanced"), 4)
    except Exception:  # noqa: BLE001 - keep seed score_min on any scorer hiccup
        pass

    def _calibrated_detect(bars):
        return det.detect(bars, thresholds=thr)

    suite = _pat_validate.run_calibration_suite(_calibrated_detect, cases)

    p.detector_thresholds = thr
    p.detector_version = getattr(det, "__version__", None)
    p.calib_pass_rate = suite["pass_rate"]
    p.calib_at = _dt.datetime.now(_dt.timezone.utc)
    db.commit()

    return JSONResponse({
        "ok": True, "readiness": _readiness(p), "suite": suite,
        "separation": thr.get("_separation", {}),
        "score_min": thr.get("score_min"),
        "thresholds": {k: v for k, v in thr.items() if not k.startswith("_")},
        "skipped": skipped, "n_pos": n_pos, "n_neg": n_neg,
    })


@router.post("/{pattern_id}/promote")
def promote_pattern(pattern_id: int, user: User = Depends(require_user),
                    db: Session = Depends(get_db)):
    """Flip a calibrated, passing pattern to `ready` (live for universe scans).
    Gated on readiness so an uncalibrated/failing detector can't go live."""
    p = _get_pattern(db, pattern_id, user)
    r = _readiness(p)
    if not r["can_promote"]:
        return JSONResponse({"ok": False, "error": r["hint"]}, status_code=400)
    p.status = PT_READY
    db.commit()
    return JSONResponse({"ok": True, "readiness": _readiness(p)})


@router.post("/{pattern_id}/reopen")
def reopen_pattern(pattern_id: int, user: User = Depends(require_user),
                   db: Session = Depends(get_db)):
    """Send a `ready` pattern back to `learning` so the user can keep teaching /
    recalibrating. Keeps the fitted thresholds intact."""
    p = _get_pattern(db, pattern_id, user)
    p.status = PT_LEARNING
    db.commit()
    return JSONResponse({"ok": True, "readiness": _readiness(p)})


@router.get("/{pattern_id}/scan")
def scan_universe(pattern_id: int,
                  tf: str = Query("daily"),
                  limit: int = Query(25),
                  user: User = Depends(require_user), db: Session = Depends(get_db)):
    """Run the calibrated detector across a CAPPED sample of the parquet universe
    — the Phase-3 'Find pattern' scan, kept synchronous + bounded for now (a full
    ~1500-symbol queued scan with progress is a later build). Honest about the cap
    in the response so the UI never implies full coverage. Ready patterns only."""
    p = _get_pattern(db, pattern_id, user)
    if p.status != PT_READY:
        return JSONResponse({"ok": False, "error": "promote the pattern first"},
                            status_code=400)
    if tf not in _TIMEFRAMES:
        tf = _TIMEFRAMES[0]
    det = _detector_for(p.slug)
    if det is None or bars_store is None:
        return JSONResponse({"ok": False, "error": "detector/bars unavailable",
                             "matches": []}, status_code=503)
    try:
        symbols = list(bars_store.list_symbols(timeframe=tf))
    except Exception:  # noqa: BLE001
        try:
            symbols = list(bars_store.list_symbols(tf))
        except Exception:
            symbols = []
    cap = max(1, min(int(limit or 25), 50))
    sample = sorted(symbols)[:cap]
    matches: list[dict] = []
    for sym in sample:
        bars = _rth_bars(_load_window(sym, tf, None, None), tf)   # detect on RTH only
        if len(bars) < _CALIB_MIN_BARS:
            continue
        try:
            found = det.detect(bars, thresholds=p.detector_thresholds or None)
        except TypeError:
            found = det.detect(bars)
        except Exception:  # noqa: BLE001
            continue
        found = _filter_valid_triangle(bars, found)   # drop support-breach breakdowns
        found = _filter_uptrend(bars, found, tf)       # uptrend-only, same EMA gate as Find
        for m in found:
            matches.append({"symbol": sym, "score": m["score"],
                            "start_t": m["start_t"], "end_t": m["end_t"],
                            "start_time": _to_lwc_time(m["start_t"], tf),
                            "end_time": _to_lwc_time(m["end_t"], tf)})
    matches.sort(key=lambda m: m["score"], reverse=True)
    return JSONResponse({
        "ok": True, "tf": tf, "scanned": len(sample),
        "universe": len(symbols), "capped": len(symbols) > len(sample),
        "count": len(matches), "matches": matches[:60],
    }, headers={"Cache-Control": "no-store"})
