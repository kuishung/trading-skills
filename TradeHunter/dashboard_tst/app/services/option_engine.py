"""The composer: one chain + its metrics + the chart state + a member's rules -> the
signal row's content (design/options/part_B_engines.md B0.1 / B3.5 / B9 ``signal_for``;
OPTIONS_MODULE_DESIGN.md II.2.6, II.5 #14).

``compute(chain, metrics, state, prefs) -> {status, headline, setup, iv, strategies,
picks, computed_ms, engine_version}`` runs the engines in order, once per
``(symbol, prefs_hash)``, for the nightly job and a Refresh:

1. ``premium_gauge`` over the ``option_metrics.all_for`` dict -> the ``iv`` dict;
2. ``strategy_rules.recommend`` over the ChartState and the gauge -> all ten rows;
3. ``strike_picker.pick`` for every recommended / also-fits rule whose picker is
   built (``step <= CURRENT_STEP``) -> ``picks = {key: [Pick]}``, the degenerate stub
   when nothing passes; the recommended row's ``why`` / ``must_happen`` are then
   re-rendered with the top pick so the strike, the expiry and the breakeven are
   real numbers;
4. ``chart_state.stored_setup`` -> the ``setup`` projection, and
   ``option_words.headline(setup, iv, strategies)`` -> the sentence, composed HERE,
   at write time, and never again.

No I/O, no DB: the nightly job commits; ``option_store.card_for`` runs the same
function lazily from the stored chain on a hash miss. ``ENGINE_VERSION`` is
stamped on every row; an older row is treated as missing.
"""
from __future__ import annotations

import logging
import time

from . import chart_state, opt_legs, option_words, premium_gauge, strategy_rules, strike_picker

log = logging.getLogger(__name__)

ENGINE_VERSION = "1.0.0"          # String(12) on the row; bump when a stored shape or a rule changes
STATUSES = ("ok", "no_setup", "no_chain", "no_iv", "stale_iv", "error")


def _expiries(chain) -> list[str]:
    if chain is None:
        return []
    fn = getattr(chain, "expiries", None)
    if callable(fn):
        try:
            return list(fn())
        except Exception:  # noqa: BLE001
            pass
    rows = chain.get("rows") if isinstance(chain, dict) else getattr(chain, "rows", None)
    if rows:
        out = set()
        for r in rows:
            e = r.get("expiry") if isinstance(r, dict) else getattr(r, "expiry", None)
            if e:
                out.add(str(e)[:10])
        return sorted(out)
    legs = chain.get("legs") if isinstance(chain, dict) else None
    if isinstance(legs, dict):
        return sorted({(k[0] if isinstance(k, tuple) else str(v.get("expiry")))
                       for k, v in legs.items() if (isinstance(k, tuple) or isinstance(v, dict))})
    return []


def _get(obj, key, default=None):
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def compute(chain, metrics: dict | None, state: dict | None, prefs: dict | None) -> dict:
    """The signal row's content for ONE ``(chain, prefs)``.

    ``chain`` - Part A's ``Chain`` (or any shape ``opt_legs.chain_view`` accepts; None
    = no chain); ``metrics`` - ``option_metrics.all_for``'s dict (None = no IV read);
    ``state`` - ``chart_state.read``'s dict (None = no chart); ``prefs`` - the
    member's merged ``option_prefs.read()`` dict.

    ``status``: ``no_chain`` (no contract rows), ``no_iv`` (no IV30 today - the gauge
    is UNKNOWN), ``no_setup`` (no setup on the chart today), else ``ok``; the rows
    are computed in every case so the card always has its ten chips and its
    gauge. An engine failure raises - the caller (the nightly job) records
    ``status="error"`` for that hash.
    """
    t0 = time.perf_counter()
    prefs = prefs or {}
    state = state or chart_state.from_stored(None)
    symbol = _get(state, "symbol") or _get(chain, "symbol") or _get(metrics, "symbol")
    if symbol and not state.get("symbol"):
        state = dict(state, symbol=symbol)
    iv = premium_gauge.from_metrics(metrics or {})
    expiries = _expiries(chain)
    rec = strategy_rules.recommend(state, iv, prefs, snapshot_expiries=expiries)
    strategies = rec["strategies"]

    today = state.get("as_of") or _get(chain, "snap_on") or _get(metrics, "snap_on")
    view = None
    if expiries:
        try:
            view = opt_legs.chain_view(chain, today=today)
            if not view.get("symbol") and symbol:
                view["symbol"] = symbol
        except Exception as exc:  # noqa: BLE001
            log.warning("option_engine %s: chain view failed: %s", symbol, exc)
            view = None

    picks: dict[str, list] = {}
    for row in strategies:
        if row["fit"] not in ("recommended", "also_fits") or row["step"] > strategy_rules.CURRENT_STEP:
            continue
        key = row["key"]
        if view is None:
            res = {"strategy": key, "family": strategy_rules.FAMILY_OF[key], "status": "degenerate",
                   "picks": [], "considered": 0, "rules_line": None,
                   "degenerate": {"reason_key": "no_chain",
                                  "text": option_words.degenerate_words("no_chain", sym=symbol or "this ticker", as_of="-"),
                                  "nearest": None, "fix": None}}
        else:
            res = strike_picker.pick(key, view, state, iv, prefs, today=today)
        picks[key] = strike_picker.stored_picks(res)
        top = next((p for p in picks[key] if p.get("status") == "ok"), None)
        if top is not None:
            why, must = strategy_rules.render(key, strategy_rules.context(state, iv, prefs, pick=top))
            row["why"], row["must_happen"] = why, must

    setup = chart_state.stored_setup(state)
    try:
        headline = option_words.headline(setup, iv, strategies)
    except Exception as exc:  # noqa: BLE001 - a wording slip must not lose the row
        log.warning("option_engine %s: headline failed: %s", symbol, exc)
        headline = None

    if not expiries:
        status = "no_chain"
    elif iv.get("verdict") == "UNKNOWN" or iv.get("iv30") is None:
        status = "no_iv"
    elif state.get("setup") is None:
        status = "no_setup"
    else:
        status = "ok"
    return {
        "status": status, "headline": headline, "setup": setup, "iv": iv,
        "strategies": strategies, "picks": picks,
        "computed_ms": int(round((time.perf_counter() - t0) * 1000.0)),
        "engine_version": ENGINE_VERSION,
        "trend": state.get("trend"), "recommended": rec.get("recommended"), "error": None,
    }
