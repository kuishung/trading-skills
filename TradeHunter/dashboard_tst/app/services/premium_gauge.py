"""The premium gauge - "is it a good time to SELL options?" - SELL / NEUTRAL / BUY /
UNKNOWN with four gates, in one pure dict (design/options/part_B_engines.md B1;
OPTIONS_MODULE_DESIGN.md II.2.6).

Inputs are the per-day figures ``iv_daily`` stores (``option_metrics.all_for``):
ALL in PERCENT (iv30 46.0, hv20 38.0, iv_front 50.0 ...). The gauge only forms
ratios and comparisons, so it never converts a unit; the per-contract ``iv`` on a
leg (a fraction) never enters it. The dict it returns IS ``option_signal.iv`` -
the writer stores it as-is.

What the verdict rests on (``basis``) is said out loud, because a rank over 34
days is a different claim from one over a year:

* ``rank``        - a full year of readings (state ``ok``) or at least 60 days
                    (``rank_ok``, the day count is in the sentence);
* ``percentile``  - 20-59 readings (``pct_only``): the percentile decides, never
                    coloured amber;
* ``provisional`` - under 20 readings but an HV20 exists: IV vs HV alone, every
                    gate CLOSED, never pushed to Telegram;
* ``unknown``     - nothing to go on: the ONE IV-unknown sentence (option_words).

IV vs HV (``iv_hv_premium``, the unitless RATIO iv30 / hv20) only MOVES the verdict
inside the 30-50 band; outside it, it is a reason - a rank of 70 with IV at 0.95x
HV is still expensive against its own year, which is the question a seller asks.

Every threshold is a rank / percentile (0-100), a ratio or a count of days -
nothing absolute (CLAUDE.md).
"""
from __future__ import annotations

from . import option_metrics, option_words
from .opt_constants import (
    BUY_MAX_RANK,
    IV_FULL_OBS,
    IV_HV_CHEAP,
    IV_HV_RICH,
    IV_MIN_OBS,
    IV_RANK_MIN_OBS,
    MID_HI,
    MID_LO,
    SELL_DIR_MIN_RANK,
    SELL_NEUTRAL_MIN_RANK,
    TERM_CONTANGO,
    TERM_EVENT,
)

VERDICTS = ("SELL", "NEUTRAL", "BUY", "UNKNOWN")
GATE_KEYS = ("buy", "sell_directional", "sell_neutral", "mid")      # exactly these four (R6)

# The keys of the iv dict, in order (II.2.6): B1.4's gauge dict + A's informational four.
IV_KEYS = (
    "iv30", "hv20", "hv60", "iv_hv_premium",
    "iv_rank", "iv_pct", "iv_n", "state", "basis", "provisional",
    "iv_front", "iv_back", "term_ratio", "skew25", "skew_norm", "expected_move",
    "earnings_date", "earnings_days",
    "verdict", "verdict_why", "gates",
    "iv30_src", "atm_iv30", "lo", "hi",
)


def _num(v):
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f else f


def _state(n: int) -> str:
    return ("none" if n == 0 else "forming" if n < IV_MIN_OBS else "pct_only" if n < IV_RANK_MIN_OBS
            else "rank_ok" if n < IV_FULL_OBS else "ok")


def _closed_gates() -> dict:
    return {k: False for k in GATE_KEYS}


def gauge(*, iv30, iv_series=None, hv20=None, hv60=None, iv_front=None, iv_back=None,
          front_dte=None, back_dte=None, skew25=None, skew_norm=None, expected_move=None,
          earnings_date=None, earnings_days=None,
          iv_rank=None, iv_pct=None, iv_n=None, lo=None, hi=None,
          iv30_src=None, atm_iv30=None) -> dict:
    """The gauge dict (= ``option_signal.iv``). PERCENT inputs throughout.

    ``iv_series`` is the daily iv30 window oldest first with TODAY included (A3.3);
    when it is given the rank / percentile are computed here, otherwise the stored
    ``iv_rank`` / ``iv_pct`` / ``iv_n`` / ``lo`` / ``hi`` are used as they come
    (``from_metrics`` passes the latter; both paths give the same partition of
    ``state`` from ``iv_n``, so a bootstrap that just landed shows on the next read).
    ``front_dte`` / ``back_dte`` are accepted for the term chip and not stored.
    """
    iv30 = _num(iv30)
    hv20, hv60 = _num(hv20), _num(hv60)
    iv_front, iv_back = _num(iv_front), _num(iv_back)
    if iv_series is not None:
        rp = option_metrics.iv_rank_pct(iv_series, iv30, hv20=hv20)
        n, rank, pct, lo, hi = rp["iv_n"], rp["iv_rank"], rp["iv_pct"], rp["lo"], rp["hi"]
    else:
        n = int(iv_n or 0)
        rank, pct = _num(iv_rank), _num(iv_pct)
        lo, hi = _num(lo), _num(hi)
    state = _state(n)
    if n < IV_RANK_MIN_OBS:
        rank = None                      # the min/max rank is not trusted under 60 readings
    if n < IV_MIN_OBS:
        pct = None
    if iv30 is None:
        rank = pct = None                # no reading today: nothing to place on the history

    if rank is not None:
        measure, basis = rank, "rank"
    elif pct is not None:
        measure, basis = pct, "percentile"
    else:
        measure, basis = None, None
    prem = round(iv30 / hv20, 4) if (iv30 and hv20) else None       # the unitless RATIO (R5)
    term = round(iv_front / iv_back, 4) if (iv_front and iv_back) else None

    why: list[str] = []
    gates = _closed_gates()
    if measure is None:
        # no usable history: IV vs HV alone, flagged provisional; the gates stay CLOSED
        if prem is None or iv30 is None:
            verdict, basis = "UNKNOWN", "unknown"
            why.append(option_words.iv_unknown_words(n))
        else:
            basis = "provisional"
            tail = f" - provisional, {n} of {IV_RANK_MIN_OBS} days of IV history"
            if prem >= IV_HV_RICH:
                verdict = "SELL"
                why.append(f"IV {iv30:.0f}% is priced for {(prem - 1) * 100:.0f}% more movement than the "
                           f"stock has actually shown ({hv20:.0f}%)" + tail)
            elif prem <= IV_HV_CHEAP:
                verdict = "BUY"
                why.append(f"IV {iv30:.0f}% is priced for {(1 - prem) * 100:.0f}% less movement than the "
                           f"stock has shown ({hv20:.0f}%)" + tail)
            else:
                verdict = "NEUTRAL"
                why.append(f"IV {iv30:.0f}% is close to the stock's own movement ({hv20:.0f}%)" + tail)
        provisional = True
    else:
        gates = {
            "buy": measure <= BUY_MAX_RANK,
            "sell_directional": measure >= SELL_DIR_MIN_RANK,
            "sell_neutral": measure >= SELL_NEUTRAL_MIN_RANK,
            "mid": MID_LO <= measure <= MID_HI,
        }
        if measure >= SELL_NEUTRAL_MIN_RANK:
            verdict = "SELL"
        elif measure >= SELL_DIR_MIN_RANK:
            verdict = "NEUTRAL"           # 30-50: either side; the ratio decides below
        else:
            verdict = "BUY"
        word = {"SELL": "expensive", "BUY": "cheap", "NEUTRAL": "middling"}[verdict]
        band = (f">= {SELL_NEUTRAL_MIN_RANK}" if verdict == "SELL"
                else f"<= {BUY_MAX_RANK}" if verdict == "BUY"
                else f"{SELL_DIR_MIN_RANK}-{SELL_NEUTRAL_MIN_RANK}")
        # the lead clause: a full-year rank names its threshold and nothing else;
        # anything shorter says the day count
        if basis == "rank":
            lead = f"IV rank {measure:.0f} ({band})" + ("" if state == "ok" else f" over {n} days")
        else:
            lead = f"Options look {word} against the last {n} days (not a full year yet)"
        if verdict == "NEUTRAL" and prem is not None and (prem >= IV_HV_RICH or prem <= IV_HV_CHEAP):
            verdict = "SELL" if prem >= IV_HV_RICH else "BUY"
            if verdict == "SELL":
                why.append(lead + f" and priced for {(prem - 1) * 100:.0f}% more movement than the stock "
                                  "has actually shown - sellers are paid")
            else:
                why.append(lead + f" and priced for {(1 - prem) * 100:.0f}% less movement than the stock "
                                  "has shown - buyers are not overcharged")
        elif prem is not None:
            more = "more" if prem > 1 else "less"
            why.append(lead + f" and priced for {abs(prem - 1) * 100:.0f}% {more} movement than the stock "
                              "has actually shown")
        else:
            why.append(lead + (f": options look {word} by this stock's own standards" if basis == "rank" else ""))
        provisional = False

    return {
        "iv30": iv30, "hv20": hv20, "hv60": hv60, "iv_hv_premium": prem,
        "iv_rank": rank, "iv_pct": pct, "iv_n": n, "state": state, "basis": basis,
        "provisional": provisional,
        "iv_front": iv_front, "iv_back": iv_back, "term_ratio": term,
        "skew25": _num(skew25), "skew_norm": _num(skew_norm), "expected_move": _num(expected_move),
        "earnings_date": earnings_date, "earnings_days": earnings_days,
        "verdict": verdict, "verdict_why": "; ".join(why), "gates": gates,
        "iv30_src": iv30_src, "atm_iv30": _num(atm_iv30), "lo": lo, "hi": hi,
    }


def from_metrics(metrics: dict) -> dict:
    """The gauge over ``option_metrics.all_for``'s dict (the iv_daily columns): the
    stored rank / percentile / day count are used as they come."""
    m = metrics or {}
    return gauge(
        iv30=m.get("iv30"), hv20=m.get("hv20"), hv60=m.get("hv60"),
        iv_front=m.get("iv_front"), iv_back=m.get("iv_back"),
        front_dte=m.get("front_dte"), back_dte=m.get("back_dte"),
        skew25=m.get("skew25"), skew_norm=m.get("skew_norm"), expected_move=m.get("expected_move"),
        earnings_date=m.get("earnings_date"), earnings_days=m.get("earnings_days"),
        iv_rank=m.get("iv_rank"), iv_pct=m.get("iv_pct"), iv_n=m.get("iv_n", m.get("n")),
        lo=m.get("iv_lo", m.get("lo")), hi=m.get("iv_hi", m.get("hi")),
        iv30_src=m.get("iv30_src"), atm_iv30=m.get("atm_iv30"),
    )


def measure(iv: dict) -> float | None:
    """What the gates were evaluated on: the rank when trusted, else the percentile;
    None when the basis is provisional / unknown."""
    iv = iv or {}
    if iv.get("basis") in ("provisional", "unknown", None):
        return None
    if iv.get("iv_rank") is not None:
        return float(iv["iv_rank"])
    if iv.get("iv_pct") is not None:
        return float(iv["iv_pct"])
    return None


def span_words(iv: dict) -> str:
    """The headline's day-count phrase: 'over the last year' (state ok), 'over {n} days'
    (rank_ok), 'against the last {n} days (not a full year yet)' (pct_only and shorter)."""
    iv = iv or {}
    n = int(iv.get("iv_n") or 0)
    state = iv.get("state")
    if state == "ok":
        return "over the last year"
    if state == "rank_ok":
        return f"over {n} days"
    return f"against the last {n} days (not a full year yet)"


def term_words(term_ratio, earnings_days=None, front_dte=None) -> str | None:
    """The term chip's text: 'front month 1.11x the back - an event is priced' (+
    ' (earnings in 19d)' when the date is known and, when ``front_dte`` is given, falls
    inside it) at or over TERM_EVENT; 'front month cheaper than the back - calendar
    shape' at or under TERM_CONTANGO; else None. Never a verdict_why clause."""
    t = _num(term_ratio)
    if t is None:
        return None
    if t >= TERM_EVENT:
        s = f"front month {t:.2f}x the back - an event is priced"
        d = earnings_days
        try:
            d = None if d is None else int(d)
        except (TypeError, ValueError):
            d = None
        if d is not None and d >= 0 and (front_dte is None or d <= int(front_dte)):
            s += f" (earnings in {d}d)"
        return s
    if t <= TERM_CONTANGO:
        return "front month cheaper than the back - calendar shape"
    return None
