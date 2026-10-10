"""Options v2 screeners - one strategy over a member's basket, by the member's rules
(OPTIONS_V2_DESIGN.md sections 8 and 13.5).

Input is what ``opt_store`` hands the page: ``chains = {sym: chain_view(...)}``
(expiries ascending, each with ``calls`` / ``puts`` rows by strike, every row
carrying its own ``as_of`` / ``source`` / ``mdt``), ``unds = {sym: underlying(...)}``
(spot, ATR / HV / IV rank from the stored daily history, earnings date) and ``rules =
opt_rules.for_strategy(...)``. No I/O here - pure functions over dicts, so a
screen is cheap enough to run on every rules change.

Prices without quotes (v4.134, §13.5). The data comes from Massive; its Options
Starter plan has NO bid/ask, so a row's ``mid`` is then the model price ``opt_massive``
stored (Black-Scholes from the contract's own IV). A leg needs a usable price (``mid``
> 0); the two bid/ask rules apply only to legs that carry a bid AND an ask - skipped
otherwise, and the funnel then gains ONE information line (``NO_QUOTES``, ``"info":
True``) whose count is the trades that reached those rules with at least one unquoted
leg (it removes nothing). ``net_natural`` (at the bid / ask) is None unless every leg
has both; ``data.priced`` (and each leg's ``priced``) is ``"quotes"`` when every price
is a bid/ask midpoint, else ``"model"``. The entry is still made in TWS at its live
price - the list finds the candidates.

Pipeline per ticker, in this order:

1. stock filters - data present, stock price, 20-day stock volume, IV rank range,
   ATR known (the families whose widths are ATR multiples), earnings date known
   (the strategy's own ``earnings_rule`` is not ``allow``). Counted in TICKERS.
2. expiry window - DTE (per role for the two-expiry families), monthly only, no
   earnings on or before the expiry (``none_inside``; ``short_leg`` checks only the
   sold, nearer expiry of a diagonal / calendar and acts like ``none_inside``
   everywhere else). Counted in EXPIRIES.
3. enumerate - the family's structures, pruned by the delta bands and the ATR
   width bands BEFORE legs are paired (so a basket screens in milliseconds);
   what the bands prune is still counted, arithmetically, in TRADES.
4. leg filters - a usable price, open interest, option volume, bid/ask $ (the
   strategy's own cap; 0 = off) and % of mid (both only on a leg with a bid and an
   ask), quote age (``max_age_h``). A trade is removed by the first rule in this order
   that any of its legs fails.
5. family rules - credit / debit / decay / time value / cost against the stock.
6. score - each ticker keeps its ``per_ticker`` best (the rest are counted too).

Every removal increments the funnel counter of the FIRST rule that removed it,
so "why so few?" has an answer per rule. A ticker that keeps nothing names the
LAST rule in the way - the furthest pipeline stage that removed anything - since
loosening that one alone lets the trades that got furthest through. Every $ figure
is per ONE contract (MULT 100); ``net`` is per share, + = credit, - = debit, at mids.

Quote age is MARKET time (``market_now``): during the US regular session it is the
wall-clock age; while the market is closed (nights, weekends, NYSE holidays -
``clock``) the clock stands still at the last close, so a quote taken at or after
that close is current (age 0) until the next open, and an older one stays as old
as it was at the close. A row's ``as_of`` is the feed's own time for that contract
(15 min delayed on the Starter plan), and the collector's last read of a day comes
after the close, so a wall clock would age Friday's closing data past 24 h every
weekend. ``data.age_min`` is that market age (the page's Data column);
``data.wall_age_min`` the plain wall-clock age (its tooltip).

POP is the risk-neutral lognormal probability (drift ``RISK_FREE``, the same
convention as ``payoff.pop``) that the stock finishes on the profitable side of
the breakeven(s) at the nearest expiry; sigma is the short leg's IV for a
credit vertical, the bought leg's for a debit / single / LEAPS / time spread,
the average of the two short legs' for a condor (falling back to the other legs,
then IV30, then HV20). Diagonal and calendar max profit, breakevens and POP come
from ``payoff`` at the near expiry (the far call model-valued), computed for the
listed trades only.
"""
from __future__ import annotations

import bisect
import datetime as _dt
import math

from . import clock, opt_rules, payoff
from .black_scholes import norm_cdf
from .opt_constants import MULT, RISK_FREE

DAYS_PER_MONTH = 30.44
EPS = 1e-9
TIME_GRID_POINTS = 81        # evenly spaced points of the near-expiry curve (plus strikes and spot)
TIME_GRID_ATR = 6.0          # ... spanning spot +/- this many ATR (5% of spot per ATR when ATR is unknown)

ATR_FAMILIES = frozenset({"debit_vertical", "credit_vertical", "condor"})
TIME_FAMILIES = frozenset({"diagonal", "calendar"})
SOLD_ROLES = frozenset({"short", "front"})      # the nearer, sold expiry of a diagonal / calendar

LEG_RULES = ("quote", "oi", "opt_vol", "spread", "spread_pct", "age")
_LEG_INDEX = {k: i for i, k in enumerate(LEG_RULES)}
_PAST_BID_ASK = _LEG_INDEX["spread_pct"]     # a first failure after this got past the bid/ask rules

# The funnel's information line (§13.5): the bid/ask rules were skipped on trades with a
# leg that has no bid and ask. It removes nothing ("info": True); its "removed" field
# holds the trades it applies to, so the page's funnel table shows that count.
NO_QUOTES = "no_quotes"
NO_QUOTES_LABEL = "Bid/ask rules not applied - your data plan has no quotes"

# enumeration (band) rules and family rules per family, in pipeline order
_BAND_RULES = {
    "single": ("delta",),
    "leaps": ("delta",),
    "debit_vertical": ("long_delta", "short_delta", "width"),
    "credit_vertical": ("short_delta", "width"),
    "condor": ("short_delta", "wing", "cross"),
    "diagonal": ("long_delta", "short_delta", "strike_order"),
    "calendar": ("atm",),
}
_FAMILY_RULES = {
    "single": ("theta",),
    "leaps": ("extrinsic",),
    "debit_vertical": ("debit",),
    "credit_vertical": ("credit",),
    "condor": ("credit",),
    "diagonal": ("debit_spot",),
    "calendar": ("iv_order", "debit"),
}


# ------------------------------------------------------------------ small helpers
def _num(v) -> float | None:
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _utc_naive(v) -> _dt.datetime | None:
    """A naive-UTC datetime from a datetime (aware converted) or an ISO string."""
    if v is None:
        return None
    if isinstance(v, str):
        try:
            v = _dt.datetime.fromisoformat(v.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    if isinstance(v, _dt.datetime):
        if v.tzinfo is not None:
            v = v.astimezone(_dt.timezone.utc).replace(tzinfo=None)
        return v
    if isinstance(v, _dt.date):
        return _dt.datetime(v.year, v.month, v.day)
    return None


def _date(v) -> _dt.date | None:
    if v is None:
        return None
    if isinstance(v, _dt.datetime):
        return v.date()
    if isinstance(v, _dt.date):
        return v
    try:
        return _dt.date.fromisoformat(str(v).strip()[:10])
    except ValueError:
        return None


def _k(strike: float) -> str:
    """A strike in an id: 330.0 -> "330", 112.5 -> "112.5"."""
    return f"{float(strike):.4f}".rstrip("0").rstrip(".")


def _g(v) -> str:
    return f"{v:g}"


def is_monthly(d: _dt.date) -> bool:
    """The third Friday of its month - or the Thursday before it when that Friday
    is an NYSE holiday (Juneteenth 2026-06-19 moves June's monthly to the 18th)."""
    if d.weekday() == 4:
        return 15 <= d.day <= 21
    if d.weekday() == 3:
        fri = d + _dt.timedelta(days=1)
        return 15 <= fri.day <= 21 and not clock.is_trading_day(fri)
    return False


def p_above(spot: float, level: float, sigma: float | None, T: float) -> float | None:
    """Risk-neutral lognormal P(S_T > level): N(d2) with drift RISK_FREE (payoff.pop's
    convention). None without a sigma or time."""
    s = _num(sigma)
    if s is None or s <= 0 or T is None or T <= 0 or not spot or spot <= 0:
        return None
    if level <= 0:
        return 1.0
    d2 = (math.log(spot / level) + (RISK_FREE - 0.5 * s * s) * T) / (s * math.sqrt(T))
    return norm_cdf(d2)


def _in(v, lo, hi) -> bool:
    return v is not None and lo - EPS <= v <= hi + EPS


# ------------------------------------------------------------------ market time
def market_now(now=None) -> _dt.datetime:
    """The clock quote ages are measured against, naive UTC: ``now`` while the US
    regular session is open; otherwise the last session's 16:00 ET close (the clock
    stands still over nights, weekends and NYSE holidays)."""
    now = _utc_naive(now) if now is not None else _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)
    if clock.us_session_open(now):
        return now
    close = clock.last_session_close(now).astimezone(_dt.timezone.utc).replace(tzinfo=None)
    return min(now, close)


def _minutes(ref: _dt.datetime, as_of: _dt.datetime) -> int:
    return max(0, int(round((ref - as_of).total_seconds() / 60.0)))


def market_age_min(as_of, now=None) -> int | None:
    """A quote's age in whole minutes of MARKET time (``market_now``): 0 for one
    taken at or after the last close while the market is closed. None when undated."""
    a = _utc_naive(as_of)
    return None if a is None else _minutes(market_now(now), a)


# ------------------------------------------------------------------ the contract
class _Opt:
    """One contract of a chain_view row with its expiry, DTE and right attached.
    ``quoted``: the row carries a bid AND an ask, so its ``mid`` is their midpoint;
    otherwise ``mid`` is the stored model price (no quotes on the data plan)."""
    __slots__ = ("exp", "dte", "right", "row", "strike", "mid", "ad", "iv", "quoted")

    def __init__(self, exp: str, dte: int, right: str, row: dict):
        self.exp, self.dte, self.right, self.row = exp, dte, right, row
        self.strike = float(row["strike"])
        mid = _num(row.get("mid"))
        bid, ask = _num(row.get("bid")), _num(row.get("ask"))
        self.quoted = bid is not None and ask is not None
        if mid is None and self.quoted:
            mid = (bid + ask) / 2.0
        self.mid = mid
        d = _num(row.get("delta"))
        self.ad = abs(d) if d is not None else None
        iv = _num(row.get("iv"))
        self.iv = iv if (iv is not None and iv > 0) else None


def _opts(exp: str, dte: int, right: str, rows) -> list[_Opt]:
    out = []
    for row in rows or ():
        if isinstance(row, dict) and _num(row.get("strike")) is not None and float(row["strike"]) > 0:
            out.append(_Opt(exp, dte, right, row))
    out.sort(key=lambda o: o.strike)
    return out


class _Exp:
    __slots__ = ("exp", "date", "dte", "roles", "_raw", "_calls", "_puts")

    def __init__(self, exp: str, date: _dt.date, dte: int, roles: set, raw: dict):
        self.exp, self.date, self.dte, self.roles, self._raw = exp, date, dte, roles, raw
        self._calls = self._puts = None

    @property
    def calls(self) -> list[_Opt]:
        if self._calls is None:
            self._calls = _opts(self.exp, self.dte, "C", self._raw.get("calls"))
        return self._calls

    @property
    def puts(self) -> list[_Opt]:
        if self._puts is None:
            self._puts = _opts(self.exp, self.dte, "P", self._raw.get("puts"))
        return self._puts

    def side(self, right: str) -> list[_Opt]:
        return self.calls if right == "C" else self.puts


# ------------------------------------------------------------------ per-ticker context
class _T:
    """Everything one ticker's screen reads, plus its counters."""

    def __init__(self, strategy: str, sym: str, chain: dict | None, und: dict | None,
                 sh: dict, r: dict, today: _dt.date, now: _dt.datetime, mnow: _dt.datetime | None = None):
        self.strategy, self.sym = strategy, sym
        self.fam = opt_rules.FAMILY[strategy]
        self.right = opt_rules.RIGHT[strategy]
        self.chain = chain if isinstance(chain, dict) else {}
        self.und = und if isinstance(und, dict) else {}
        self.sh, self.r = sh, r
        self.today, self.now = today, now
        self.mnow = mnow if mnow is not None else market_now(now)     # quote ages count market time
        self.spot = _num(self.und.get("spot")) or _num(self.chain.get("spot"))
        self.atr = _num(self.und.get("atr14"))
        self.iv30 = _num(self.und.get("iv30"))
        self.hv20 = _num(self.und.get("hv20"))
        self.earnings = _date(self.und.get("earnings_date"))
        self.max_age = _dt.timedelta(hours=float(sh["max_age_h"]))
        self.leg_cache: dict[int, str] = {}
        self.counts: dict[str, int] = {}
        self.no_quotes = 0        # trades that reached the bid/ask rules with an unquoted leg
        self.total = 0
        self.reason: str | None = None

    def count(self, key: str, n: int = 1) -> None:
        if n:
            self.counts[key] = self.counts.get(key, 0) + n

    def sigma_fallback(self) -> float | None:
        if self.iv30 and self.iv30 > 0:
            return self.iv30 / 100.0
        if self.hv20 and self.hv20 > 0:
            return self.hv20 / 100.0
        return None


def _rules(strategy: str, rules: dict | None) -> tuple[dict, dict]:
    """``for_strategy`` output (or a partial one, or ``opt_rules.read`` output) ->
    (shared, strategy rules), every field present and in bounds."""
    rules = rules if isinstance(rules, dict) else {}
    own = rules.get("rules") if "rules" in rules else rules.get(strategy)
    clean = opt_rules.for_strategy({"shared": rules.get("shared"), strategy: own}, strategy)
    return clean["shared"], clean["rules"]


# ------------------------------------------------------------------ rule labels
def _iv_active(r: dict) -> bool:
    return r["iv_rank_min"] > 0 or r["iv_rank_max"] < 100


def _earnings_active(r: dict) -> bool:
    return r["earnings_rule"] != "allow"


def _sold_leg_only(fam: str, r: dict) -> bool:
    """``short_leg`` on a two-expiry family: only the sold (nearer) expiry is checked."""
    return r["earnings_rule"] == "short_leg" and fam in TIME_FAMILIES


def _earnings_label(fam: str, r: dict) -> str:
    if _sold_leg_only(fam, r):
        return "Earnings on or before the sold call's expiry"
    return "Earnings on or before the expiry"


def _windows(fam: str, r: dict) -> dict[str, tuple[float, float]]:
    """role -> (lo, hi) DTE window."""
    if fam == "leaps":
        return {"main": (r["months_lo"] * DAYS_PER_MONTH, r["months_hi"] * DAYS_PER_MONTH)}
    if fam == "diagonal":
        return {"short": (r["short_dte_lo"], r["short_dte_hi"]), "long": (r["long_dte_lo"], r["long_dte_hi"])}
    if fam == "calendar":
        return {"front": (r["front_dte_lo"], r["front_dte_hi"]), "back": (r["back_dte_lo"], r["back_dte_hi"])}
    return {"main": (r["dte_lo"], r["dte_hi"])}


def _dte_label(fam: str, r: dict) -> str:
    if fam == "leaps":
        return f"Expiry not {r['months_lo']}-{r['months_hi']} months away"
    if fam == "diagonal":
        return (f"Expiry in neither window (sold call {r['short_dte_lo']}-{r['short_dte_hi']} days, "
                f"bought call {r['long_dte_lo']}-{r['long_dte_hi']} days)")
    if fam == "calendar":
        return (f"Expiry in neither window (near {r['front_dte_lo']}-{r['front_dte_hi']} days, "
                f"far {r['back_dte_lo']}-{r['back_dte_hi']} days)")
    return f"Expiry not {r['dte_lo']}-{r['dte_hi']} days away"


def _band_label(key: str, fam: str, r: dict) -> str:
    if key == "delta":
        return f"Delta outside {r['delta_lo']:.2f}-{r['delta_hi']:.2f} (or not reported)"
    if key == "long_delta":
        who = "Bought call" if fam == "diagonal" else "Bought option"
        return f"{who} delta outside {r['long_delta_lo']:.2f}-{r['long_delta_hi']:.2f} (or not reported)"
    if key == "short_delta":
        who = {"diagonal": "Sold call", "condor": "A sold option's"}.get(fam, "Sold option")
        return f"{who} delta outside {r['short_delta_lo']:.2f}-{r['short_delta_hi']:.2f} (or not reported)"
    if key == "width":
        return f"Distance between strikes outside {_g(r['width_atr_lo'])}-{_g(r['width_atr_hi'])} x ATR"
    if key == "wing":
        return f"A wing outside {_g(r['wing_atr_lo'])}-{_g(r['wing_atr_hi'])} x ATR"
    if key == "cross":
        return "Sold put not below the sold call"
    if key == "strike_order":
        return "Sold call strike not above the bought call strike"
    if key == "atm":
        return f"Not the call nearest delta 0.50 (within {r['delta_tol']:.2f})"
    return key


def _leg_label(key: str, sh: dict, r: dict) -> str:
    return {
        "quote": "No price on an option (no bid/ask and no IV)",
        "oi": f"Open interest under {sh['oi_min']:,} on an option (or not reported)",
        "opt_vol": f"Volume today under {sh['opt_vol_min']:,} on an option (or not reported)",
        "spread": f"Bid/ask wider than ${r['max_leg_spread']:.2f} on an option",
        "spread_pct": f"Bid/ask wider than {_g(sh['max_leg_spread_pct'])}% of the price on an option",
        "age": (f"Quote older than {_g(sh['max_age_h'])} h on an option (or undated; "
                "the clock stops while the market is closed)"),
    }[key]


def _leg_rule_active(key: str, sh: dict, r: dict) -> bool:
    if key == "oi":
        return sh["oi_min"] > 0
    if key == "opt_vol":
        return sh["opt_vol_min"] > 0
    if key == "spread":
        return r["max_leg_spread"] > 0
    return True


def _family_label(key: str, fam: str, r: dict) -> str:
    if key == "theta":
        return f"Daily time decay over {_g(r['theta_pct_max'])}% of the price (or not reported)"
    if key == "extrinsic":
        return f"Time value over {_g(r['extrinsic_pct_max'])}% of the price"
    if key == "debit" and fam == "debit_vertical":
        return f"Cost over {_g(r['debit_pct_max'])}% of the distance between strikes"
    if key == "debit":
        return "Far option not dearer than the near one (no net cost)"
    if key == "credit":
        return f"Credit under {_g(r['credit_pct_min'])}% of the max loss"
    if key == "debit_spot":
        return f"Net cost over {_g(r['debit_pct_spot_max'])}% of the stock price"
    if key == "iv_order":
        return "Near option's IV below the far option's"
    return key


def catalog(strategy: str, sh: dict, r: dict) -> list[tuple[str, str, str]]:
    """``(rule, unit, label)`` for every ACTIVE rule of ``strategy``, in pipeline
    order - the funnel's rows. Units: tickers / expiries / trades."""
    fam = opt_rules.FAMILY[strategy]
    earn = _earnings_active(r)
    out = [("data", "tickers", "No option data or stock price yet")]
    if sh["price_min"] > 0:
        out.append(("price_min", "tickers", f"Stock price under ${_g(sh['price_min'])}"))
    if sh["stock_vol_min"] > 0:
        out.append(("stock_vol_min", "tickers",
                    f"20-day stock volume under {sh['stock_vol_min']:,} shares (or not known)"))
    if _iv_active(r):
        out.append(("iv_rank", "tickers", f"IV rank outside {r['iv_rank_min']}-{r['iv_rank_max']} (or not known)"))
    if fam in ATR_FAMILIES:
        out.append(("atr", "tickers", "ATR not known yet"))
    if earn:
        out.append(("earnings_known", "tickers", "Next earnings date not known"))
    out.append(("dte", "expiries", _dte_label(fam, r)))
    if sh["monthly_only"]:
        out.append(("monthly", "expiries", "Not a monthly expiry"))
    if earn:
        out.append(("earnings", "expiries", _earnings_label(fam, r)))
    for k in _BAND_RULES[fam]:
        out.append((k, "trades", _band_label(k, fam, r)))
    for k in LEG_RULES:
        if _leg_rule_active(k, sh, r):
            out.append((k, "trades", _leg_label(k, sh, r)))
    for k in _FAMILY_RULES[fam]:
        if k == "iv_order" and not r.get("front_iv_ge_back"):
            continue
        out.append((k, "trades", _family_label(k, fam, r)))
    out.append(("per_ticker", "trades", f"Beyond the best {sh['per_ticker']} per stock"))
    return out


# ------------------------------------------------------------------ stage 1: the stock
def _stock_fail(t: _T) -> tuple[str, str] | None:
    """(rule, plain reason) of the first stock filter the ticker fails, or None."""
    sh, r = t.sh, t.r
    if not t.chain.get("expiries") or not t.spot or t.spot <= 0:
        return "data", "no option data yet"
    if sh["price_min"] > 0 and t.spot < sh["price_min"] - EPS:
        return "price_min", f"stock price {t.spot:.2f} is under {_g(sh['price_min'])}"
    if sh["stock_vol_min"] > 0:
        v = _num(t.und.get("avg_vol20"))
        if v is None:
            return "stock_vol_min", "20-day stock volume not known yet"
        if v < sh["stock_vol_min"]:
            return "stock_vol_min", f"20-day stock volume {v:,.0f} is under {sh['stock_vol_min']:,}"
    if _iv_active(r):
        ivr = _num(t.und.get("iv_rank"))
        if ivr is None:
            return "iv_rank", "IV rank not known yet"
        if not (r["iv_rank_min"] - EPS <= ivr <= r["iv_rank_max"] + EPS):
            return "iv_rank", f"IV rank {ivr:.0f} is outside {r['iv_rank_min']}-{r['iv_rank_max']}"
    if t.fam in ATR_FAMILIES and not (t.atr and t.atr > 0):
        return "atr", "ATR not known yet"
    if _earnings_active(r) and (t.earnings is None or t.earnings < t.today):
        return "earnings_known", "earnings date unknown"
    return None


# ------------------------------------------------------------------ stage 2: expiries
def _window_roles(windows: dict, dte: int) -> set:
    return {role for role, (lo, hi) in windows.items() if lo - EPS <= dte <= hi + EPS}


def _expiry_fail(t: _T, d: _dt.date, dte: int, windows: dict) -> tuple[str | None, set]:
    """(first expiry rule ``d`` fails or None, the roles it can still play). With
    ``short_leg`` on a diagonal / calendar a report on or before ``d`` only takes
    away its sold role - it may still be the far, bought leg."""
    roles = _window_roles(windows, dte)
    if not roles:
        return "dte", roles
    if t.sh["monthly_only"] and not is_monthly(d):
        return "monthly", roles
    if _earnings_active(t.r) and t.earnings is not None and t.earnings <= d:
        roles = roles - SOLD_ROLES if _sold_leg_only(t.fam, t.r) else set()
        if not roles:
            return "earnings", roles
    return None, roles


def _expiries(t: _T) -> list[_Exp]:
    windows = _windows(t.fam, t.r)
    out = []
    for e in t.chain.get("expiries") or ():
        if not isinstance(e, dict):
            continue
        d = _date(e.get("expiry"))
        if d is None:
            continue
        dte = (d - t.today).days
        if dte < 0:
            continue
        fail, roles = _expiry_fail(t, d, dte, windows)
        if fail:
            t.count(fail)
            continue
        out.append(_Exp(d.isoformat(), d, dte, roles, e))
    return out


# ------------------------------------------------------------------ stage 4: the legs
def _leg_check(t: _T, o: _Opt) -> str:
    """The first leg rule ``o`` fails, "" when it passes them all. A usable price is a
    ``mid`` > 0 (a bid/ask midpoint, or the model price on a plan without quotes; a
    crossed or negative quote is no price). The bid/ask $ and % rules check only a leg
    that has both a bid and an ask."""
    sh, row = t.sh, o.row
    bid, ask = _num(row.get("bid")), _num(row.get("ask"))
    if o.mid is None or o.mid <= 0 or (o.quoted and (bid < 0 or ask <= 0 or ask < bid)):
        return "quote"
    if sh["oi_min"] > 0:
        oi = _num(row.get("oi"))
        if oi is None or oi < sh["oi_min"]:
            return "oi"
    if sh["opt_vol_min"] > 0:
        v = _num(row.get("volume"))
        if v is None or v < sh["opt_vol_min"]:
            return "opt_vol"
    if o.quoted:
        width = ask - bid
        cap = t.r["max_leg_spread"]                  # the strategy's own $ band; 0 = off
        if cap > 0 and width > cap + EPS:
            return "spread"
        if width / o.mid * 100.0 > sh["max_leg_spread_pct"] + EPS:
            return "spread_pct"
    as_of = _utc_naive(row.get("as_of"))
    if as_of is None or t.mnow - as_of > t.max_age:          # market time (market_now)
        return "age"
    return ""


def _leg_fail(t: _T, legs) -> str | None:
    """The first leg rule (in ``LEG_RULES`` order) any of ``legs`` fails, or None. A
    trade that got past the bid/ask rules (no failure, or only a later one) while one
    of its legs had no bid and ask is counted in ``t.no_quotes`` - those rules were not
    applied to it."""
    best = None
    unquoted = False
    for o in legs:
        key = id(o.row)
        f = t.leg_cache.get(key)
        if f is None:
            f = t.leg_cache[key] = _leg_check(t, o)
        if f and (best is None or _LEG_INDEX[f] < _LEG_INDEX[best]):
            best = f
        unquoted = unquoted or not o.quoted
    if unquoted and (best is None or _LEG_INDEX[best] > _PAST_BID_ASK):
        t.no_quotes += 1
    return best


# ------------------------------------------------------------------ the candidate
def _src(row: dict) -> str:
    mdt = row.get("mdt") or "unknown"
    if row.get("source") == "member":
        name = row.get("source_name") or (f"#{row.get('source_user_id')}" if row.get("source_user_id") else "member")
        return f"member:{name}·{mdt}"
    return f"{row.get('source') or 'unknown'}·{mdt}"


def _leg(o: _Opt, side: str) -> dict:
    row = o.row
    return {"expiry": o.exp, "right": o.right, "strike": o.strike, "side": side, "qty": 1,
            "bid": _num(row.get("bid")), "ask": _num(row.get("ask")), "mid": o.mid, "price": o.mid,
            "priced": "quotes" if o.quoted else "model",
            "iv": o.iv, "delta": _num(row.get("delta")), "theta": _num(row.get("theta")),
            "oi": _num(row.get("oi")), "volume": _num(row.get("volume")), "dte": o.dte,
            "as_of": row.get("as_of"), "source": row.get("source"), "source_user_id": row.get("source_user_id"),
            "source_name": row.get("source_name"), "mdt": row.get("mdt")}


def _r(v, nd: int):
    return None if v is None else round(v, nd)


def _cand(t: _T, legs: list[tuple[_Opt, str]], *, net, net_natural, max_profit, max_loss, breakevens,
          pop, ror, score, metrics) -> dict:
    lg = [_leg(o, side) for o, side in legs]
    opts = [o for o, _ in legs]
    quoted = all(o.quoted for o in opts)
    if not quoted:
        net_natural = None              # at the bid / ask only when every leg has both
    cid = "|".join([t.sym, t.strategy] + [f"{o.exp}|{o.right}|{_k(o.strike)}" for o in opts])
    ois = [l["oi"] for l in lg]
    spreads = [(l["ask"] - l["bid"]) if (l["ask"] is not None and l["bid"] is not None) else None for l in lg]
    pcts = [s / l["mid"] * 100.0 if (s is not None and l["mid"]) else None for s, l in zip(spreads, lg)]
    vols = [l["volume"] for l in lg]
    stamps = [_utc_naive(l["as_of"]) for l in lg]
    oldest = None if any(s is None for s in stamps) else min(stamps)
    # the oldest leg has the largest age on both clocks (market age = max(0, mnow - as_of))
    age = None if oldest is None else _minutes(t.mnow, oldest)
    wall_age = None if oldest is None else _minutes(t.now, oldest)
    sources = []
    for o in opts:
        s = _src(o.row)
        if s not in sources:
            sources.append(s)
    return {
        "id": cid, "symbol": t.sym, "strategy": t.strategy, "legs": lg,
        "dte": min(o.dte for o in opts),
        "net": _r(net, 4), "net_natural": _r(net_natural, 4),
        "max_profit": _r(max_profit, 2), "max_loss": _r(max_loss, 2),
        "breakevens": [round(b, 2) for b in breakevens],
        "pop": _r(pop, 4), "ror": _r(ror, 4), "score": round(score, 6),
        "metrics": {k: (_r(v, 4) if isinstance(v, float) else v) for k, v in metrics.items()},
        "liquidity": {"oi_min": None if any(v is None for v in ois) else min(ois),
                      "spread_max": None if any(v is None for v in spreads) else round(max(spreads), 4),
                      "spread_pct_max": None if any(v is None for v in pcts) else round(max(pcts), 2),
                      "volume_min": None if any(v is None for v in vols) else min(vols)},
        "data": {"as_of_oldest": oldest, "age_min": age, "wall_age_min": wall_age, "sources": sources,
                 "mixed": len(sources) > 1, "priced": "quotes" if quoted else "model"},
        "underlying": {"spot": t.spot, "iv_rank": _num(t.und.get("iv_rank")), "iv30": t.iv30, "hv20": t.hv20,
                       "atr14": t.atr, "earnings_date": t.und.get("earnings_date")},
    }


def _sigma(t: _T, first: list[_Opt], rest: list[_Opt]) -> float | None:
    """The average IV of ``first`` legs that have one, else of ``rest``, else IV30 /
    HV20."""
    for group in (first, rest):
        ivs = [o.iv for o in group if o.iv]
        if ivs:
            return sum(ivs) / len(ivs)
    return t.sigma_fallback()


def _nat_buy(o: _Opt) -> float:
    return _num(o.row.get("ask"))


def _nat_sell(o: _Opt) -> float:
    return _num(o.row.get("bid"))


# ---- single (buy_call / buy_put) and leaps
def _make_single(t: _T, o: _Opt) -> dict | None:
    prem = o.mid
    if prem is None or prem <= 0:
        return None
    if o.right == "C":
        be, max_profit, ror = o.strike + prem, None, None
    else:
        be, max_profit, ror = o.strike - prem, (o.strike - prem) * MULT, (o.strike - prem) / prem
    p = p_above(t.spot, be, _sigma(t, [o], []), o.dte / 365.0)
    pop = p if (o.right == "C" or p is None) else 1.0 - p
    theta = _num(o.row.get("theta"))
    theta_pct = abs(theta) / prem * 100.0 if theta is not None else None
    intrinsic = max(0.0, t.spot - o.strike) if o.right == "C" else max(0.0, o.strike - t.spot)
    extrinsic = max(0.0, prem - intrinsic)
    extrinsic_pct = extrinsic / prem * 100.0
    r = t.r
    mid_band = (r["delta_lo"] + r["delta_hi"]) / 2.0
    dist = abs((o.ad if o.ad is not None else mid_band) - mid_band) / 100.0
    if t.fam == "leaps":
        score = -extrinsic_pct - dist
    else:
        score = -(theta_pct if theta_pct is not None else 1e6) - dist
    return _cand(t, [(o, "buy")], net=-prem, net_natural=-_nat_buy(o) if _nat_buy(o) is not None else None,
                 max_profit=max_profit, max_loss=prem * MULT, breakevens=[be], pop=pop, ror=ror, score=score,
                 metrics={"premium": prem, "delta": o.ad, "theta_pct": theta_pct, "extrinsic": extrinsic,
                          "extrinsic_pct": extrinsic_pct, "intrinsic": intrinsic})


def _fail_single(t: _T, c: dict) -> str | None:
    if t.fam == "leaps":
        return "extrinsic" if c["metrics"]["extrinsic_pct"] > t.r["extrinsic_pct_max"] + EPS else None
    tp = c["metrics"]["theta_pct"]
    return "theta" if tp is None or tp > t.r["theta_pct_max"] + EPS else None


def _enum_single(t: _T, exps: list[_Exp]) -> list[dict]:
    r, out = t.r, []
    for e in exps:
        for o in e.side(t.right):
            t.total += 1
            if not _in(o.ad, r["delta_lo"], r["delta_hi"]):
                t.count("delta")
                continue
            f = _leg_fail(t, (o,))
            if f:
                t.count(f)
                continue
            c = _make_single(t, o)
            f = "quote" if c is None else _fail_single(t, c)
            if f:
                t.count(f)
                continue
            out.append(c)
    return out


# ---- verticals
def _further_otm(rows: list[_Opt], i: int, right: str) -> range:
    """Indices of the strikes further out of the money than ``rows[i]`` (higher
    for calls, lower for puts)."""
    return range(i + 1, len(rows)) if right == "C" else range(0, i)


def _make_debit(t: _T, lo: _Opt, so: _Opt) -> dict | None:
    if lo.mid is None or so.mid is None:
        return None
    width = abs(so.strike - lo.strike)
    debit = lo.mid - so.mid
    be = lo.strike + debit if lo.right == "C" else lo.strike - debit
    p = p_above(t.spot, be, _sigma(t, [lo], [so]), lo.dte / 365.0)
    pop = p if lo.right == "C" else (None if p is None else 1.0 - p)
    ok = debit > 0 and width > debit
    ror = (width - debit) / debit if ok else None
    nb, ns = _nat_buy(lo), _nat_sell(so)
    return _cand(t, [(lo, "buy"), (so, "sell")], net=-debit,
                 net_natural=-(nb - ns) if (nb is not None and ns is not None) else None,
                 max_profit=(width - debit) * MULT, max_loss=debit * MULT, breakevens=[be], pop=pop, ror=ror,
                 score=(ror or 0.0) * (pop or 0.0),
                 metrics={"debit": debit, "debit_pct": debit / width * 100.0 if width else None, "width": width,
                          "width_atr": width / t.atr if t.atr else None, "long_delta": lo.ad, "short_delta": so.ad})


def _fail_debit(t: _T, c: dict) -> str | None:
    m = c["metrics"]
    if m["debit"] <= 0 or m["debit"] >= m["width"] or m["debit_pct"] > t.r["debit_pct_max"] + EPS:
        return "debit"
    return None


def _enum_debit(t: _T, exps: list[_Exp]) -> list[dict]:
    r, out, right = t.r, [], t.right
    wlo, whi = r["width_atr_lo"] * t.atr, r["width_atr_hi"] * t.atr
    for e in exps:
        rows = e.side(right)
        n = len(rows)
        short_ok = [_in(o.ad, r["short_delta_lo"], r["short_delta_hi"]) for o in rows]
        for i, lo in enumerate(rows):
            partners = (n - 1 - i) if right == "C" else i
            t.total += partners
            if not _in(lo.ad, r["long_delta_lo"], r["long_delta_hi"]):
                t.count("long_delta", partners)
                continue
            shorts = [rows[j] for j in _further_otm(rows, i, right) if short_ok[j]]
            t.count("short_delta", partners - len(shorts))
            for so in shorts:
                if not _in(abs(so.strike - lo.strike), wlo, whi):
                    t.count("width")
                    continue
                f = _leg_fail(t, (lo, so))
                if f:
                    t.count(f)
                    continue
                c = _make_debit(t, lo, so)
                f = "quote" if c is None else _fail_debit(t, c)
                if f:
                    t.count(f)
                    continue
                out.append(c)
    return out


def _make_credit(t: _T, so: _Opt, lo: _Opt) -> dict | None:
    if so.mid is None or lo.mid is None:
        return None
    width = abs(so.strike - lo.strike)
    credit = so.mid - lo.mid
    risk = width - credit
    be = so.strike - credit if so.right == "P" else so.strike + credit
    p = p_above(t.spot, be, _sigma(t, [so], [lo]), so.dte / 365.0)
    pop = p if so.right == "P" else (None if p is None else 1.0 - p)
    ok = credit > 0 and risk > 0
    ror = credit / risk if ok else None
    ns, nb = _nat_sell(so), _nat_buy(lo)
    return _cand(t, [(so, "sell"), (lo, "buy")], net=credit,
                 net_natural=(ns - nb) if (ns is not None and nb is not None) else None,
                 max_profit=credit * MULT, max_loss=risk * MULT, breakevens=[be], pop=pop, ror=ror,
                 score=(ror or 0.0) * (pop or 0.0),
                 metrics={"credit": credit, "credit_pct": credit / risk * 100.0 if ok else None, "width": width,
                          "width_atr": width / t.atr if t.atr else None, "short_delta": so.ad, "long_delta": lo.ad})


def _fail_credit(t: _T, c: dict) -> str | None:
    pct = c["metrics"]["credit_pct"]
    return "credit" if pct is None or pct < t.r["credit_pct_min"] - EPS else None


def _credit_pairs(t: _T, rows: list[_Opt], right: str, d_lo: float, d_hi: float, w_lo: float, w_hi: float,
                  count: bool) -> tuple[int, int, list[tuple[_Opt, _Opt]]]:
    """Every (short, long further OTM) pair of one side: returns (all pairs, pairs
    whose short is in the delta band, the pairs that also pass the width band).
    ``count`` books the band removals on ``t`` (a vertical; a condor books its own)."""
    n = len(rows)
    strikes = [o.strike for o in rows]
    total = n * (n - 1) // 2
    in_band = 0
    ok: list[tuple[_Opt, _Opt]] = []
    for i, so in enumerate(rows):
        partners = (n - 1 - i) if right == "C" else i
        if not _in(so.ad, d_lo, d_hi):
            if count:
                t.count("short_delta", partners)
            continue
        in_band += partners
        if right == "P":
            a = bisect.bisect_left(strikes, so.strike - w_hi - EPS, 0, i)
            b = bisect.bisect_right(strikes, so.strike - w_lo + EPS, 0, i)
        else:
            a = bisect.bisect_left(strikes, so.strike + w_lo - EPS, i + 1, n)
            b = bisect.bisect_right(strikes, so.strike + w_hi + EPS, i + 1, n)
        longs = rows[a:b] if b > a else []
        if count:
            t.count("width", partners - len(longs))
        ok.extend((so, lo) for lo in longs)
    return total, in_band, ok


def _enum_credit(t: _T, exps: list[_Exp]) -> list[dict]:
    r, out, right = t.r, [], t.right
    wlo, whi = r["width_atr_lo"] * t.atr, r["width_atr_hi"] * t.atr
    for e in exps:
        total, _, pairs = _credit_pairs(t, e.side(right), right, r["short_delta_lo"], r["short_delta_hi"],
                                        wlo, whi, True)
        t.total += total
        for so, lo in pairs:
            f = _leg_fail(t, (so, lo))
            if f:
                t.count(f)
                continue
            c = _make_credit(t, so, lo)
            f = "quote" if c is None else _fail_credit(t, c)
            if f:
                t.count(f)
                continue
            out.append(c)
    return out


# ---- condor
def _make_condor(t: _T, pl: _Opt, ps: _Opt, cs: _Opt, cl: _Opt) -> dict | None:
    if any(o.mid is None for o in (pl, ps, cs, cl)):
        return None
    credit = (ps.mid - pl.mid) + (cs.mid - cl.mid)
    wp, wc = ps.strike - pl.strike, cl.strike - cs.strike
    wing = max(wp, wc)
    risk = wing - credit
    be_lo, be_hi = ps.strike - credit, cs.strike + credit
    sig = _sigma(t, [ps, cs], [pl, cl])
    T = ps.dte / 365.0
    a, b = p_above(t.spot, be_lo, sig, T), p_above(t.spot, be_hi, sig, T)
    pop = None if (a is None or b is None) else max(0.0, a - b)
    ok = credit > 0 and risk > 0
    ror = credit / risk if ok else None
    nat = [_nat_sell(ps), _nat_buy(pl), _nat_sell(cs), _nat_buy(cl)]
    return _cand(t, [(pl, "buy"), (ps, "sell"), (cs, "sell"), (cl, "buy")], net=credit,
                 net_natural=(nat[0] - nat[1] + nat[2] - nat[3]) if all(v is not None for v in nat) else None,
                 max_profit=credit * MULT, max_loss=risk * MULT, breakevens=[be_lo, be_hi], pop=pop, ror=ror,
                 score=(ror or 0.0) * (pop or 0.0),
                 metrics={"credit": credit, "credit_pct": credit / risk * 100.0 if ok else None,
                          "put_width": wp, "call_width": wc, "wing": wing,
                          "wing_atr": wing / t.atr if t.atr else None,
                          "short_put_delta": ps.ad, "short_call_delta": cs.ad})


def _enum_condor(t: _T, exps: list[_Exp]) -> list[dict]:
    r, out = t.r, []
    wlo, whi = r["wing_atr_lo"] * t.atr, r["wing_atr_hi"] * t.atr
    dlo, dhi = r["short_delta_lo"], r["short_delta_hi"]
    for e in exps:
        p_all, p_band, p_ok = _credit_pairs(t, e.puts, "P", dlo, dhi, wlo, whi, False)
        c_all, c_band, c_ok = _credit_pairs(t, e.calls, "C", dlo, dhi, wlo, whi, False)
        t.total += p_all * c_all
        t.count("short_delta", p_all * c_all - p_band * c_band)
        t.count("wing", p_band * c_band - len(p_ok) * len(c_ok))
        for ps, pl in p_ok:
            for cs, cl in c_ok:
                if ps.strike >= cs.strike:
                    t.count("cross")
                    continue
                f = _leg_fail(t, (pl, ps, cs, cl))
                if f:
                    t.count(f)
                    continue
                c = _make_condor(t, pl, ps, cs, cl)
                f = "quote" if c is None else _fail_credit(t, c)
                if f:
                    t.count(f)
                    continue
                out.append(c)
    return out


# ---- diagonal and calendar (two expiries)
def _make_diagonal(t: _T, lo: _Opt, so: _Opt) -> dict | None:
    if lo.mid is None or so.mid is None:
        return None
    debit = lo.mid - so.mid
    nb, ns = _nat_buy(lo), _nat_sell(so)
    per_delta = debit / lo.ad if (lo.ad and debit > 0) else None
    return _cand(t, [(lo, "buy"), (so, "sell")], net=-debit,
                 net_natural=-(nb - ns) if (nb is not None and ns is not None) else None,
                 max_profit=None, max_loss=debit * MULT, breakevens=[], pop=None, ror=None,
                 score=-(per_delta if per_delta is not None else 1e9),
                 metrics={"debit": debit, "debit_pct_spot": debit / t.spot * 100.0, "debit_per_delta": per_delta,
                          "long_delta": lo.ad, "short_delta": so.ad,
                          "max_loss_basis": "net debit (approximate: the far call is model-valued)"})


def _fail_diagonal(t: _T, c: dict) -> str | None:
    m = c["metrics"]
    return "debit_spot" if m["debit"] <= 0 or m["debit_pct_spot"] > t.r["debit_pct_spot_max"] + EPS else None


def _enum_diagonal(t: _T, exps: list[_Exp]) -> list[dict]:
    r, out = t.r, []
    shorts_e = [e for e in exps if "short" in e.roles]
    for el in (e for e in exps if "long" in e.roles):
        earlier = [e for e in shorts_e if e.date < el.date]
        n_partners = sum(len(e.calls) for e in earlier)
        band = [o for e in earlier for o in e.calls if _in(o.ad, r["short_delta_lo"], r["short_delta_hi"])]
        for lo in el.calls:
            t.total += n_partners
            if not _in(lo.ad, r["long_delta_lo"], r["long_delta_hi"]):
                t.count("long_delta", n_partners)
                continue
            t.count("short_delta", n_partners - len(band))
            for so in band:
                if so.strike <= lo.strike + EPS:
                    t.count("strike_order")
                    continue
                f = _leg_fail(t, (lo, so))
                if f:
                    t.count(f)
                    continue
                c = _make_diagonal(t, lo, so)
                f = "quote" if c is None else _fail_diagonal(t, c)
                if f:
                    t.count(f)
                    continue
                out.append(c)
    return out


def _make_calendar(t: _T, fo: _Opt, bo: _Opt) -> dict | None:
    if fo.mid is None or bo.mid is None:
        return None
    debit = bo.mid - fo.mid
    gap = bo.dte - fo.dte
    nb, ns = _nat_buy(bo), _nat_sell(fo)
    per_day = debit / gap if gap > 0 else None
    return _cand(t, [(fo, "sell"), (bo, "buy")], net=-debit,
                 net_natural=-(nb - ns) if (nb is not None and ns is not None) else None,
                 max_profit=None, max_loss=debit * MULT, breakevens=[], pop=None, ror=None,
                 score=-(per_day if (per_day is not None and debit > 0) else 1e9),
                 metrics={"debit": debit, "debit_per_day": per_day, "front_iv": fo.iv, "back_iv": bo.iv,
                          "front_delta": fo.ad, "days_between": gap})


def _fail_calendar(t: _T, c: dict) -> str | None:
    m = c["metrics"]
    if t.r["front_iv_ge_back"] and (m["front_iv"] is None or m["back_iv"] is None
                                    or m["front_iv"] < m["back_iv"] - EPS):
        return "iv_order"
    if m["debit"] <= 0 or m["days_between"] <= 0:
        return "debit"
    return None


def _atm_call(rows: list[_Opt], tol: float) -> _Opt | None:
    """The call whose delta is nearest 0.50 (lower strike on a tie), when within
    ``tol`` of it."""
    best = None
    for o in rows:
        if o.ad is None:
            continue
        if best is None or abs(o.ad - 0.5) < abs(best.ad - 0.5) - EPS:
            best = o
    return best if (best is not None and abs(best.ad - 0.5) <= tol + EPS) else None


def _enum_calendar(t: _T, exps: list[_Exp]) -> list[dict]:
    r, out = t.r, []
    backs_all = [e for e in exps if "back" in e.roles]
    for ef in (e for e in exps if "front" in e.roles):
        backs = [(e, {round(o.strike, 4): o for o in e.calls}) for e in backs_all if e.date > ef.date]
        atm = _atm_call(ef.calls, r["delta_tol"])
        for fo in ef.calls:
            key = round(fo.strike, 4)
            partners = [m[key] for _, m in backs if key in m]
            t.total += len(partners)
            if fo is not atm:
                t.count("atm", len(partners))
                continue
            for bo in partners:
                f = _leg_fail(t, (fo, bo))
                if f:
                    t.count(f)
                    continue
                c = _make_calendar(t, fo, bo)
                f = "quote" if c is None else _fail_calendar(t, c)
                if f:
                    t.count(f)
                    continue
                out.append(c)
    return out


def _complete_time(t: _T, c: dict) -> dict:
    """Max profit, breakevens, POP and RoR of a diagonal / calendar from the
    near-expiry P&L curve (payoff: the near leg at intrinsic, the far leg
    Black-Scholes-valued with its own IV, else IV30 / HV20)."""
    fb = t.sigma_fallback()
    legs = []
    for l in c["legs"]:
        iv = l["iv"] or fb
        legs.append(payoff.Leg(right=l["right"], strike=l["strike"], expiry=l["expiry"],
                               qty=1 if l["side"] == "buy" else -1, price=l["mid"], iv=iv, delta=l["delta"]))
    try:
        _, days = payoff.horizon(legs, t.today)
        unit = t.atr if (t.atr and t.atr > 0) else 0.05 * t.spot
        lo = max(t.spot - TIME_GRID_ATR * unit, 0.01)
        hi = t.spot + TIME_GRID_ATR * unit
        xs = {round(lo + (hi - lo) * i / (TIME_GRID_POINTS - 1), 4) for i in range(TIME_GRID_POINTS)}
        xs.update(round(l.strike, 4) for l in legs)
        xs.add(round(t.spot, 4))
        xs = sorted(xs)
        ys = payoff.expiry_curve(legs, xs, t.today)
        bes = payoff.breakevens(xs, ys, legs, t.today)
        if bes:
            merged = dict(zip(xs, ys))
            for b in bes:
                merged.setdefault(round(b, 4), 0.0)
            xs = sorted(merged)
            ys = [merged[x] for x in xs]
        long_leg = next((l for l in legs if l.qty > 0), legs[0])
        sigma = long_leg.iv
        pop = payoff.pop("time", legs, t.spot, sigma, days / 365.0, xs, ys)
        max_profit = max(ys)
    except (ValueError, TypeError, ZeroDivisionError):
        return c
    c["max_profit"] = round(max_profit, 2)
    c["breakevens"] = [round(b, 2) for b in bes]
    c["pop"] = _r(pop, 4)
    c["ror"] = _r(max_profit / c["max_loss"], 4) if c["max_loss"] and c["max_loss"] > 0 else None
    return c


_ENUM = {"single": _enum_single, "leaps": _enum_single, "debit_vertical": _enum_debit,
         "credit_vertical": _enum_credit, "condor": _enum_condor, "diagonal": _enum_diagonal,
         "calendar": _enum_calendar}


# ------------------------------------------------------------------ the screen
def _defaults_time(today, now) -> tuple[_dt.date, _dt.datetime]:
    now = _utc_naive(now) if now is not None else _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)
    today = _date(today) if today is not None else clock.et_date(now)
    return today, now


def _order(c: dict) -> tuple:
    return (-c["score"], c["id"])


def _lc(label: str) -> str:
    """A label inside a sentence: only its first letter lowered ("ATR", "IV", "$" kept)."""
    return label[:1].lower() + label[1:]


_EXPIRY_RULES = ("dte", "monthly", "earnings")


def _no_trade_reason(t: _T, cat: list, labels: dict, exps: list) -> str:
    """Why a ticker that passed the stock filters keeps no trade, in plain words.

    Trades: the LAST rule in the way - walking the trade rules backwards through the
    pipeline (family rules, then leg rules, then band rules), the first that removed
    anything. Every trade it removed got past every earlier rule, so loosening it
    alone lets them through; the band rules (which prune the most, by far) are named
    only when nothing got past the bands. Expiries: the last expiry rule that removed
    any (an expiry only reaches the monthly / earnings checks from inside the DTE
    window, so "earnings" is the real blocker even when most were simply too near /
    far) - for a diagonal / calendar that also covers "none left for one of the two
    legs"."""
    order = [k for k, unit, _ in cat if unit == "trades" and k != "per_ticker"]
    last = next((k for k in reversed(order) if t.counts.get(k)), None)
    if last is None:            # a count outside the catalog (never expected): the largest
        rest = {k: v for k, v in t.counts.items() if k not in _EXPIRY_RULES and k != "per_ticker" and v}
        last = max(rest, key=rest.get) if rest else None
    if last is not None:
        return "no trade passes: the last rule in the way is " + _lc(labels.get(last, "the rules"))
    exp_rules = [k for k in _EXPIRY_RULES if t.counts.get(k)]
    if exp_rules:
        return "no expiry passes: " + _lc(labels[exp_rules[-1]])
    return "no trades to consider" if exps else "no expiries listed"


def screen(strategy: str, chains: dict, unds: dict, rules: dict | None, *, today=None, now=None) -> dict:
    """One strategy over every ticker in ``chains`` / ``unds``: the trades that pass
    every rule (each ticker's ``per_ticker`` best, best score first), the funnel
    (every active rule with how many tickers / expiries / trades it removed - the
    first rule each failed - plus, when any trade had a leg without a bid and ask, the
    ``NO_QUOTES`` line after the bid/ask rules: ``"info": True``, ``"removed"`` = the
    trades those rules were not applied to; it removes nothing, so leave it out of any
    sum), per-ticker ``{"passed", "reason"}``, ``n_considered`` (trades enumerated
    before any band) and ``n_passed`` (trades listed)."""
    if strategy not in opt_rules.STRATEGIES:
        raise KeyError(strategy)
    sh, r = _rules(strategy, rules)
    today, now = _defaults_time(today, now)
    mnow = market_now(now)
    chains = chains if isinstance(chains, dict) else {}
    unds = unds if isinstance(unds, dict) else {}
    cat = catalog(strategy, sh, r)
    labels = {k: lbl for k, _, lbl in cat}
    counts: dict[str, int] = {}
    rows: list[dict] = []
    tickers: dict[str, dict] = {}
    n_considered = 0
    n_no_quotes = 0
    for sym in sorted(set(chains) | set(unds)):
        t = _T(strategy, sym, chains.get(sym), unds.get(sym), sh, r, today, now, mnow)
        fail = _stock_fail(t)
        if fail:
            t.count(fail[0])
            tickers[sym] = {"passed": 0, "reason": fail[1]}
        else:
            exps = _expiries(t)
            found = _ENUM[t.fam](t, exps) if exps else []
            found.sort(key=_order)
            keep = found[: sh["per_ticker"]]
            t.count("per_ticker", len(found) - len(keep))
            if t.fam in TIME_FAMILIES:
                keep = [_complete_time(t, c) for c in keep]
            rows.extend(keep)
            n_considered += t.total
            reason = None if keep else _no_trade_reason(t, cat, labels, exps)
            tickers[sym] = {"passed": len(keep), "reason": reason}
        for k, v in t.counts.items():
            counts[k] = counts.get(k, 0) + v
        n_no_quotes += t.no_quotes
    rows.sort(key=_order)
    funnel = [{"rule": k, "label": lbl, "unit": unit, "removed": counts.get(k, 0)} for k, unit, lbl in cat]
    if n_no_quotes:
        # right after the bid/ask rules it speaks for; it removed nothing ("info")
        at = next((i + 1 for i, f in enumerate(funnel) if f["rule"] == "spread_pct"), len(funnel))
        funnel.insert(at, {"rule": NO_QUOTES, "label": NO_QUOTES_LABEL, "unit": "trades",
                           "removed": n_no_quotes, "info": True})
    return {"rows": rows, "funnel": funnel, "tickers": tickers,
            "n_considered": n_considered, "n_passed": len(rows), "strategy": strategy}


# ------------------------------------------------------------------ one trade, re-derived
def parse_id(candidate_id: str) -> tuple[str, str, list[tuple[str, str, float]]] | None:
    """``"SYM|strategy|expiry|right|strike|..."`` -> (symbol, strategy, legs) or None."""
    parts = str(candidate_id or "").split("|")
    if len(parts) < 5 or (len(parts) - 2) % 3:
        return None
    legs = []
    for i in range(2, len(parts), 3):
        exp, right, k = parts[i], parts[i + 1].upper(), _num(parts[i + 2])
        if _date(exp) is None or right not in ("C", "P") or k is None:
            return None
        legs.append((_date(exp).isoformat(), right, k))
    return parts[0], parts[1], legs


_SHAPE = {"single": 1, "leaps": 1, "debit_vertical": 2, "credit_vertical": 2, "diagonal": 2, "calendar": 2,
          "condor": 4}


def _find(t: _T, exp: str, right: str, strike: float) -> tuple[_Exp, _Opt] | None:
    windows = _windows(t.fam, t.r)
    for e in t.chain.get("expiries") or ():
        d = _date((e or {}).get("expiry"))
        if d is None or d.isoformat() != exp:
            continue
        dte = (d - t.today).days
        _, roles = _expiry_fail(t, d, dte, windows)
        ex = _Exp(exp, d, dte, roles, e)
        for o in ex.side(right):
            if abs(o.strike - strike) < 1e-6:
                return ex, o
    return None


def _band_fail(t: _T, exps: list[_Exp], opts: list[_Opt]) -> str | None:
    """The first band rule a given structure fails (the enumerator's prunes, for
    one structure)."""
    r, fam = t.r, t.fam
    if fam in ("single", "leaps"):
        return None if _in(opts[0].ad, r["delta_lo"], r["delta_hi"]) else "delta"
    if fam == "debit_vertical":
        lo, so = opts
        if not _in(lo.ad, r["long_delta_lo"], r["long_delta_hi"]):
            return "long_delta"
        if not _in(so.ad, r["short_delta_lo"], r["short_delta_hi"]):
            return "short_delta"
        return None if _in(abs(so.strike - lo.strike), r["width_atr_lo"] * t.atr, r["width_atr_hi"] * t.atr) \
            else "width"
    if fam == "credit_vertical":
        so, lo = opts
        if not _in(so.ad, r["short_delta_lo"], r["short_delta_hi"]):
            return "short_delta"
        return None if _in(abs(so.strike - lo.strike), r["width_atr_lo"] * t.atr, r["width_atr_hi"] * t.atr) \
            else "width"
    if fam == "condor":
        pl, ps, cs, cl = opts
        if not (_in(ps.ad, r["short_delta_lo"], r["short_delta_hi"])
                and _in(cs.ad, r["short_delta_lo"], r["short_delta_hi"])):
            return "short_delta"
        wlo, whi = r["wing_atr_lo"] * t.atr, r["wing_atr_hi"] * t.atr
        if not (_in(ps.strike - pl.strike, wlo, whi) and _in(cl.strike - cs.strike, wlo, whi)):
            return "wing"
        return "cross" if ps.strike >= cs.strike else None
    if fam == "diagonal":
        lo, so = opts
        if not _in(lo.ad, r["long_delta_lo"], r["long_delta_hi"]):
            return "long_delta"
        if not _in(so.ad, r["short_delta_lo"], r["short_delta_hi"]):
            return "short_delta"
        return "strike_order" if so.strike <= lo.strike + EPS else None
    if fam == "calendar":
        fo = opts[0]
        return None if _atm_call(exps[0].calls, r["delta_tol"]) is fo else "atm"
    return None


def _structure_fail(t: _T, exps: list[_Exp], opts: list[_Opt], c: dict) -> str | None:
    """The first rule (stock, expiry, band, leg, family) the structure fails."""
    fail = _stock_fail(t)
    if fail:
        return fail[0]
    need = {"diagonal": ("long", "short"), "calendar": ("front", "back")}.get(t.fam)
    windows = _windows(t.fam, t.r)
    for i, e in enumerate(exps):
        f, roles = _expiry_fail(t, e.date, e.dte, windows)
        if not f and need and need[i] not in roles:
            # in its window but its role taken away by a report (short_leg) -> earnings
            f = "earnings" if need[i] in _window_roles(windows, e.dte) else "dte"
        if f:
            return f
    if t.fam == "diagonal" and not exps[1].date < exps[0].date:
        return "dte"
    if t.fam == "calendar" and not exps[0].date < exps[1].date:
        return "dte"
    f = _band_fail(t, exps, opts) or _leg_fail(t, opts)
    if f:
        return f
    fam_fail = {"single": _fail_single, "leaps": _fail_single, "debit_vertical": _fail_debit,
                "credit_vertical": _fail_credit, "condor": _fail_credit, "diagonal": _fail_diagonal,
                "calendar": _fail_calendar}[t.fam]
    return fam_fail(t, c)


def detail(strategy: str, candidate_id: str, chain: dict | None, und: dict | None, rules: dict | None, *,
           today=None, now=None) -> dict | None:
    """One trade re-derived from the current chain: ``{"candidate", "payoff",
    "fails"}`` - the candidate with today's quotes, ``payoff.build`` for it, and
    the first rule it now fails (``{"rule", "label"}``) or None. None when the id
    is not this strategy's, or a leg is no longer in the chain / has no price."""
    parsed = parse_id(candidate_id)
    if parsed is None or strategy not in opt_rules.STRATEGIES:
        return None
    sym, strat, legs = parsed
    if strat != strategy:
        return None
    sh, r = _rules(strategy, rules)
    today, now = _defaults_time(today, now)
    t = _T(strategy, sym, chain, und, sh, r, today, now, market_now(now))
    if not t.spot or len(legs) != _SHAPE[t.fam]:
        return None
    found = [_find(t, *leg) for leg in legs]
    if any(f is None for f in found):
        return None
    exps = [f[0] for f in found]
    opts = [f[1] for f in found]
    if t.right != "CP" and any(o.right != t.right for o in opts):
        return None
    if t.fam in ATR_FAMILIES and not (t.atr and t.atr > 0):
        t.atr = None
    maker = {"single": _make_single, "leaps": _make_single, "debit_vertical": _make_debit,
             "credit_vertical": _make_credit, "condor": _make_condor, "diagonal": _make_diagonal,
             "calendar": _make_calendar}[t.fam]
    try:
        c = maker(t, *opts)
    except (TypeError, ZeroDivisionError):
        c = None
    if c is None:
        return None
    if t.fam in TIME_FAMILIES:
        c = _complete_time(t, c)
    if t.fam in ATR_FAMILIES and t.atr is None:
        fail = "atr"
    else:
        fail = _structure_fail(t, exps, opts, c)
    labels = {k: lbl for k, _, lbl in catalog(strategy, sh, r)}
    fb = t.sigma_fallback()
    po = payoff.build(c["legs"], strategy=strategy, spot=t.spot, atr=t.atr, as_of=today,
                      sigma_fallback=fb, symbol=sym)
    return {"candidate": c, "payoff": po,
            "fails": None if not fail else {"rule": fail, "label": labels.get(fail, fail)}}
