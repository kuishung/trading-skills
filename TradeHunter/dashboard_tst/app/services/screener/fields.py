"""The screener's field registry (OPTIONS_SCREENER_DESIGN.md §5): every figure a member can
filter on, sort by or see as a column, with how to compute it.

``Field(key, label, group, level, kind, unit, fmt, help, presets, choices)``

* ``level`` - where the value lives:
    ``underlying`` (one per symbol, ``frame.u``), ``contract`` (one per option, ``frame.c``),
    ``leg`` (a contract field of one leg of a strategy, addressed ``leg1.volume``,
    ``leg2.delta`` ...), ``strategy`` (computed per combination / per single-leg trade).
* ``kind`` - how it is filtered: ``range`` (gte / lte / between / eq), ``choice`` (in / eq),
  ``bool`` (is / eq), ``date`` (in / eq / between / within / gte / lte).
* ``unit`` - ``$``, ``%``, ``days``, ``x`` (a ratio), ``contracts``, ``shares``, ``""``.
* ``fmt`` - how the page prints it: ``text``, ``int``, ``num2``, ``num4``, ``pct`` (the value
  is already in percent: 12.5 means 12.5 %), ``money`` (2 decimals), ``ratio``, ``date``
  (YYYY-MM-DD), ``datetime`` (ISO UTC, ``Z``), ``bool``.

There are NO bid / ask fields anywhere (the user's decision, §0): every option price is the
estimated price from the contract's own IV, labelled *est.*

Also here: ``Table`` (the rows a screen produces - one contract-index array per leg plus the
computed strategy metrics), filter parsing (``parse_filters``) and the mask / sort helpers the
single-leg and strategy runners share.
"""
from __future__ import annotations

import datetime as _dt
import re
from dataclasses import dataclass
from functools import lru_cache

import numpy as np

from .frame import EPOCH, EXCHANGES, SEC_TYPES, TRENDS, Frame

GROUPS = ("Option Info", "Option Analysis", "Options Overview", "Break Even Analysis",
          "Price & Volume", "Technical Analysis", "Profile")
LEVELS = ("underlying", "contract", "leg", "strategy")
OPS_BY_KIND = {
    "range": ("gte", "lte", "between", "eq"),
    "choice": ("in", "eq"),
    "bool": ("is", "eq"),
    "date": ("in", "eq", "between", "within", "gte", "lte"),
}
ALL_OPS = ("gte", "lte", "eq", "between", "in", "is", "within")


@dataclass(frozen=True)
class Field:
    key: str
    label: str
    group: str
    level: str
    kind: str = "range"
    unit: str = ""
    fmt: str = "num2"
    help: str = ""
    presets: tuple = ()
    choices: tuple = ()
    src: str = ""          # the frame array (contract / underlying level) or metric key
    base: str = ""         # leg fields: the contract field they read
    leg: int = 0           # leg fields: 1-based leg number
    within: str = ""       # date fields: what "within N" counts - "days" or "hours"

    @property
    def ops(self) -> tuple:
        return OPS_BY_KIND[self.kind]

    def public(self, frame: Frame | None = None) -> dict:
        d = {"key": self.key, "label": self.label, "group": self.group, "level": self.level,
             "kind": self.kind, "unit": self.unit, "fmt": self.fmt, "help": self.help,
             "ops": list(self.ops), "presets": [dict(p) for p in self.presets],
             "choices": [dict(c) for c in self.choices]}
        if self.within:
            d["within"] = self.within
        if frame is not None and (self.key == "expiry" or self.base == "expiry") and not frame.empty:
            exps = frame.cached("expiries", lambda: [_iso_day(x) for x in np.unique(frame.c["exp"]).tolist()])
            d["choices"] = [{"v": x, "label": x} for x in exps]
        return d


# ─────────────────────────────────── presets & choices ───────────────────────────────────

def _rng(label, lo, hi):
    return {"label": label, "lo": lo, "hi": hi}


DTE_PRESETS = (_rng("< 60", None, 60), _rng("60-100", 60, 100), _rng("100-150", 100, 150),
               _rng("150-200", 150, 200), _rng("> 200", 200, None))
VOL_PRESETS = (_rng("Very Low", 0, 100), _rng("Low", 100, 250), _rng("Average", 250, 500),
               _rng("High", 500, 1000), _rng("Very High", 1000, None))
MNY_PRESETS = (_rng("Deep OTM", None, -25), _rng("OTM", -25, -5), _rng("ATM", -5, 5),
               _rng("ITM", 5, 25), _rng("Deep ITM", 25, None))
IVR_PRESETS = (_rng("Low", 0, 30), _rng("Mid", 30, 50), _rng("High", 50, 100))
RSI_PRESETS = (_rng("Oversold", None, 30), _rng("Neutral", 30, 70), _rng("Overbought", 70, None))
PROB_PRESETS = (_rng("< 30", None, 30), _rng("30-50", 30, 50), _rng("50-70", 50, 70),
                _rng("70-85", 70, 85), _rng("> 85", 85, None))
DELTA_PRESETS = (_rng("Deep put", -1, -0.7), _rng("Put", -0.7, -0.3), _rng("Low", -0.3, 0.3),
                 _rng("Call", 0.3, 0.7), _rng("Deep call", 0.7, 1))
HOURS_PRESETS = ({"label": "1 hour", "v": 1}, {"label": "4 hours", "v": 4},
                 {"label": "24 hours", "v": 24}, {"label": "3 days", "v": 72})
DAYS_PRESETS = ({"label": "7 days", "v": 7}, {"label": "14 days", "v": 14},
                {"label": "30 days", "v": 30}, {"label": "60 days", "v": 60})

RIGHT_CHOICES = ({"v": "C", "label": "Call"}, {"v": "P", "label": "Put"})
CYCLE_CHOICES = ({"v": "monthly", "label": "Monthly"}, {"v": "weekly", "label": "Weekly"})
SEC_CHOICES = tuple({"v": v, "label": lbl} for v, lbl in zip(SEC_TYPES, ("Stock", "ETF", "Index", "Other")))
EXCH_CHOICES = tuple({"v": v, "label": v.title() if v == "OTHER" else v} for v in EXCHANGES)
TREND_CHOICES = tuple({"v": v, "label": v.title()} for v in TRENDS)


# ─────────────────────────────────── the registry ───────────────────────────────────

_F: dict[str, Field] = {}


def _add(key, label, group, level, *, src=None, **kw) -> None:
    _F[key] = Field(key, label, group, level, src=src or key, **kw)


# ---- Option Info (contract) ----
_g = "Option Info"
_add("option_type", "Option Type", _g, "contract", kind="choice", fmt="text", src="is_put_f",
     choices=RIGHT_CHOICES, help="Call or put.")
_add("expiry", "Expiration Date", _g, "contract", kind="date", fmt="date", src="exp_f", within="days",
     help="The expiration date. 'within N' = expires in the next N days.")
_add("dte", "Days to Expiration", _g, "contract", unit="days", fmt="int", presets=DTE_PRESETS,
     help="Calendar days from today (ET) to expiration.")
_add("expiry_type", "Monthly / Weekly", _g, "contract", kind="choice", fmt="text", src="weekly",
     choices=CYCLE_CHOICES, help="Standard monthly expiration (third Friday) or a weekly / other.")
_add("strike", "Strike Price", _g, "contract", unit="$", fmt="money")
_add("price", "Price (est.)", _g, "contract", unit="$", fmt="money",
     help="Estimated from the contract's own IV (Black-Scholes); our data has no bid/ask. "
          "Falls back to the last trade.")
_add("last", "Last", _g, "contract", unit="$", fmt="money", help="Last trade of the session.")
_add("opt_chg_pct", "Option % Change", _g, "contract", unit="%", fmt="pct", src="chg_pct")
_add("volume", "Option Volume", _g, "contract", unit="contracts", fmt="int", presets=VOL_PRESETS)
_add("vol_chg_pct", "Volume % Change", _g, "contract", unit="%", fmt="pct",
     help="Today's volume against the previous session's.")
_add("oi", "Open Interest", _g, "contract", unit="contracts", fmt="int", presets=VOL_PRESETS)
_add("oi_chg", "Open Interest Change", _g, "contract", unit="contracts", fmt="int")
_add("oi_chg_pct", "Open Interest % Change", _g, "contract", unit="%", fmt="pct")
_add("vol_oi", "Volume / Open Interest", _g, "contract", unit="x", fmt="ratio")
_add("premium", "Premium", _g, "contract", unit="$", fmt="money",
     help="Dollar value traded today: est. price x 100 x volume.")
_add("last_trade", "Last Trade", _g, "contract", kind="date", fmt="datetime", within="hours",
     presets=HOURS_PRESETS, help="'within N' = traded in the N hours before the data's read time.")
_add("exp_before_earnings", "Expires Before Earnings", _g, "contract", kind="bool", fmt="bool",
     src="exp_before_earn", help="No known earnings date between today and expiration.")
_add("earnings_before_exp", "Earnings Before Expiration", _g, "contract", kind="bool", fmt="bool",
     src="earn_before", help="A known earnings date falls between today and expiration.")

# ---- Option Analysis (contract) ----
_g = "Option Analysis"
_add("moneyness", "Moneyness", _g, "contract", unit="%", fmt="pct", presets=MNY_PRESETS,
     help="Calls (S-K)/S, puts (K-S)/S, in percent: positive = in the money.")
_add("iv", "Implied Volatility", _g, "contract", unit="%", fmt="pct", src="iv_pct")
_add("delta", "Delta", _g, "contract", fmt="num4", presets=DELTA_PRESETS, help="Signed: puts negative.")
_add("gamma", "Gamma", _g, "contract", fmt="num4")
_add("theta", "Theta", _g, "contract", fmt="num4")
_add("vega", "Vega", _g, "contract", fmt="num4")
_add("profit_prob", "Profit Probability", _g, "contract", unit="%", fmt="pct", presets=PROB_PRESETS,
     help="Buying the option: chance the stock ends past the break-even (lognormal, from the IV).")
_add("otm_prob", "OTM Probability", _g, "contract", unit="%", fmt="pct", presets=PROB_PRESETS,
     help="Chance the option expires out of the money.")
_add("itm_prob", "ITM Probability", _g, "contract", unit="%", fmt="pct", presets=PROB_PRESETS)
_add("tp", "Time Premium", _g, "contract", unit="$", fmt="money", help="Est. price minus intrinsic value.")
_add("tp_pct", "%Time Premium", _g, "contract", unit="%", fmt="pct",
     help="Time premium as a percent of the stock price.")
_add("dist_strike_pct", "Distance from Strike %", _g, "contract", unit="%", fmt="pct",
     help="(Strike - stock price) / stock price.")
_add("iv_hv", "IV / HV", _g, "contract", unit="x", fmt="ratio", help="The option's IV over the 20-day HV.")
_add("exp_move", "Expected Move", _g, "contract", unit="$", fmt="money",
     help="One standard deviation to expiration: price x IV x sqrt(DTE/365).")
_add("exp_move_pct", "Expected Move %", _g, "contract", unit="%", fmt="pct")

# ---- Break Even Analysis (contract) ----
_g = "Break Even Analysis"
_add("breakeven", "Break Even", _g, "contract", unit="$", fmt="money",
     help="Buying the option: strike + price (call), strike - price (put).")
_add("breakeven_pct", "%Break Even", _g, "contract", unit="%", fmt="pct",
     help="How far the stock must move to the break-even.")

# ---- Options Overview (underlying) ----
_g = "Options Overview"
_add("iv_rank", "IV Rank", _g, "underlying", unit="%", fmt="pct", presets=IVR_PRESETS,
     help="Where IV30 sits in its 1-year range (blank until the IV history is read).")
_add("iv_pctl", "IV Percentile", _g, "underlying", unit="%", fmt="pct", presets=IVR_PRESETS)
_add("iv30", "IV30", _g, "underlying", unit="%", fmt="pct", help="30-day constant-maturity IV.")
_add("iv_chg", "IV30 Change", _g, "underlying", unit="%", fmt="pct",
     help="IV30 change since the previous session, in points.")
_add("iv30_hv20", "IV30 / HV20", _g, "underlying", unit="x", fmt="ratio")
_add("exp_move30", "Expected Move (30d) %", _g, "underlying", unit="%", fmt="pct")
_add("call_vol", "Total Call Volume", _g, "underlying", unit="contracts", fmt="int")
_add("put_vol", "Total Put Volume", _g, "underlying", unit="contracts", fmt="int")
_add("total_vol", "Total Option Volume", _g, "underlying", unit="contracts", fmt="int")
_add("pc_vol", "Put/Call Volume Ratio", _g, "underlying", unit="x", fmt="ratio")
_add("call_oi", "Total Call Open Interest", _g, "underlying", unit="contracts", fmt="int")
_add("put_oi", "Total Put Open Interest", _g, "underlying", unit="contracts", fmt="int")
_add("total_oi", "Total Open Interest", _g, "underlying", unit="contracts", fmt="int")
_add("pc_oi", "Put/Call OI Ratio", _g, "underlying", unit="x", fmt="ratio")
_add("vol_oi_total", "Total Volume / Open Interest", _g, "underlying", unit="x", fmt="ratio")

# ---- Price & Volume (underlying) ----
_g = "Price & Volume"
_add("stock_price", "Price", _g, "underlying", unit="$", fmt="money", src="spot",
     help="The underlying's price (15-min delayed, or the last close).")
_add("stock_chg_pct", "% Change", _g, "underlying", unit="%", fmt="pct", src="chg_pct")
_add("stock_volume", "Stock Volume", _g, "underlying", unit="shares", fmt="int")
_add("avg_vol20", "20-Day Avg Volume", _g, "underlying", unit="shares", fmt="int")
_add("avg_vol50", "50-Day Avg Volume", _g, "underlying", unit="shares", fmt="int")

# ---- Technical Analysis (underlying) ----
_g = "Technical Analysis"
_add("pct_sma20", "% from 20-Day SMA", _g, "underlying", unit="%", fmt="pct")
_add("pct_sma50", "% from 50-Day SMA", _g, "underlying", unit="%", fmt="pct")
_add("pct_sma200", "% from 200-Day SMA", _g, "underlying", unit="%", fmt="pct")
_add("rsi14", "14-Day RSI", _g, "underlying", fmt="num2", presets=RSI_PRESETS)
_add("atr_pct", "ATR %", _g, "underlying", unit="%", fmt="pct", help="14-day ATR as a percent of price.")
_add("hv20", "20-Day Historic Volatility", _g, "underlying", unit="%", fmt="pct")
_add("hv60", "60-Day Historic Volatility", _g, "underlying", unit="%", fmt="pct")
_add("pct_hi52", "% from 52-Week High", _g, "underlying", unit="%", fmt="pct")
_add("pct_lo52", "% from 52-Week Low", _g, "underlying", unit="%", fmt="pct")
_add("perf5", "5-Day % Change", _g, "underlying", unit="%", fmt="pct")
_add("perf20", "20-Day % Change", _g, "underlying", unit="%", fmt="pct")
_add("trend", "Trend", _g, "underlying", kind="choice", fmt="text", src="trend_f", choices=TREND_CHOICES,
     help="Up / down / sideways - the MATP trend rule on daily bars.")

# ---- Profile (underlying) ----
_g = "Profile"
_add("symbol", "Symbol", _g, "underlying", kind="choice", fmt="text", src="sym_code",
     help="Limit to these tickers.")
_add("exchange", "Exchange", _g, "underlying", kind="choice", fmt="text", src="exchange_f",
     choices=EXCH_CHOICES)
_add("sec_type", "Security Type", _g, "underlying", kind="choice", fmt="text", src="sec_type_f",
     choices=SEC_CHOICES)
_add("earnings_date", "Earnings Date", _g, "underlying", kind="date", fmt="date", src="earn_day",
     within="days", presets=DAYS_PRESETS, help="Next earnings date. 'within N' = in the next N days.")
_add("days_to_earnings", "Days to Earnings", _g, "underlying", unit="days", fmt="int", src="days_to_earn")

# ---- strategy level (computed per trade) ----
_g = "Option Analysis"
_add("net_debit", "Net Debit", _g, "strategy", unit="$", fmt="money",
     help="Paid to open, per contract (x100). Negative = a credit.")
_add("net_credit", "Net Credit", _g, "strategy", unit="$", fmt="money",
     help="Received to open, per contract (x100). Negative = a debit.")
_add("width", "Strike Width", _g, "strategy", unit="$", fmt="money", help="Strike distance, per share.")
_add("max_profit", "Max Profit", _g, "strategy", unit="$", fmt="money",
     help="Per contract (x100). Blank = unlimited.")
_add("max_loss", "Max Loss", _g, "strategy", unit="$", fmt="money",
     help="Per contract (x100). Blank = unlimited.")
_add("max_profit_pct", "Max Profit %", _g, "strategy", unit="%", fmt="pct", help="Max profit / max loss.")
_add("risk_reward", "Risk/Reward", _g, "strategy", unit="x", fmt="ratio", help="Max loss / max profit.")
_add("win_prob", "Profit Prob", _g, "strategy", unit="%", fmt="pct", presets=PROB_PRESETS,
     help="Chance the trade ends profitable at expiration (lognormal, IV of the leg nearest each "
          "break-even).")
_add("loss_prob", "Loss Prob", _g, "strategy", unit="%", fmt="pct", presets=PROB_PRESETS,
     help="Chance the trade ends at a loss at expiration.")
_add("max_profit_prob", "Max Profit Prob", _g, "strategy", unit="%", fmt="pct",
     help="Chance the stock ends where the full max profit is earned.")
_add("net_delta", "Net Delta", _g, "strategy", fmt="num4", help="Per share, buy legs +, sell legs -.")
_add("net_gamma", "Net Gamma", _g, "strategy", fmt="num4")
_add("net_theta", "Net Theta", _g, "strategy", fmt="num4")
_add("net_vega", "Net Vega", _g, "strategy", fmt="num4")
_add("iv_skew", "IV Skew", _g, "strategy", unit="%", fmt="pct",
     help="Near leg IV minus far leg IV, in points.")
_add("avg_iv_hv", "IV/HV", _g, "strategy", unit="x", fmt="ratio", help="Mean leg IV over the 20-day HV.")
_add("return_pct", "Return", _g, "strategy", unit="%", fmt="pct",
     help="Covered call: (price - ITM amount) / (stock - price). Naked put: price / (strike - price).")
_add("ann_return", "Annualized Return", _g, "strategy", unit="%", fmt="pct", help="Return x 365 / DTE.")
_add("ptnl_return", "Potential Return", _g, "strategy", unit="%", fmt="pct",
     help="Covered call if called away: (price + upside to the strike) / (stock - price).")
_add("downside_pct", "Downside", _g, "strategy", unit="%", fmt="pct",
     help="Most the position can lose, as a percent of its cost.")
_add("upside_pct", "Upside", _g, "strategy", unit="%", fmt="pct",
     help="Most the position can gain, as a percent of its cost.")
_add("cost_pct", "%Cost", _g, "strategy", unit="%", fmt="pct",
     help="Net option cost as a percent of the stock price (negative = a credit).")
_g = "Break Even Analysis"
_add("be", "Break Even", _g, "strategy", unit="$", fmt="money")
_add("be_pct", "%Break Even", _g, "strategy", unit="%", fmt="pct")
_add("be_hi", "Break Even +", _g, "strategy", unit="$", fmt="money", help="The upper break-even.")
_add("be_hi_pct", "%Break Even +", _g, "strategy", unit="%", fmt="pct")
_add("be_lo", "Break Even -", _g, "strategy", unit="$", fmt="money", help="The lower break-even.")
_add("be_lo_pct", "%Break Even -", _g, "strategy", unit="%", fmt="pct")
del _g

# leg bases a strategy screen offers as leg filters (columns may use any contract field)
LEG_FILTER_BASES = ("volume", "oi", "moneyness", "delta", "iv", "strike", "price", "otm_prob")
LEG_TERM_BASES = ("dte", "expiry")          # added for calendars / diagonals (legs differ in expiry)
_LEG_RE = re.compile(r"^leg([1-4])\.([a-z0-9_]+)$")


@lru_cache(maxsize=512)
def get(key: str) -> Field | None:
    """The field for ``key`` (including ``legN.<contract field>``), or None."""
    if not isinstance(key, str):
        return None
    f = _F.get(key)
    if f is not None:
        return f
    m = _LEG_RE.match(key)
    if not m:
        return None
    base = _F.get(m.group(2))
    if base is None or base.level != "contract":
        return None
    n = int(m.group(1))
    return Field(key=key, label=f"{base.label} Leg {n}", group=base.group, level="leg", kind=base.kind,
                 unit=base.unit, fmt=base.fmt, help=base.help, presets=base.presets,
                 choices=base.choices, src=base.src, base=base.key, leg=n, within=base.within)


def all_fields(level: str | None = None) -> list[Field]:
    return [f for f in _F.values() if level is None or f.level == level]


# ─────────────────────────────────── the table ───────────────────────────────────

class Table:
    """The rows a screen produced: ``legs[k]`` = contract indices of leg k+1 (single-leg
    screens have one), ``m`` = strategy-level metric arrays, all of length ``n``."""

    __slots__ = ("frame", "legs", "m")

    def __init__(self, frame: Frame, legs: list, m: dict | None = None):
        self.frame = frame
        self.legs = [np.asarray(x, dtype=np.int64) for x in legs]
        self.m = m if m is not None else {}

    @property
    def n(self) -> int:
        return int(len(self.legs[0])) if self.legs else 0

    @property
    def sym(self) -> np.ndarray:
        return self.frame.c["sym"][self.legs[0]]

    def take(self, sel) -> "Table":
        return Table(self.frame, [x[sel] for x in self.legs], {k: v[sel] for k, v in self.m.items()})

    @staticmethod
    def concat(parts: list["Table"]) -> "Table":
        parts = [p for p in parts if p is not None]
        if len(parts) == 1:
            return parts[0]
        first = parts[0]
        legs = [np.concatenate([p.legs[k] for p in parts]) for k in range(len(first.legs))]
        keys = set().union(*(p.m.keys() for p in parts))
        m = {}
        for k in keys:
            m[k] = np.concatenate([p.m.get(k, np.full(p.n, np.nan)) for p in parts])
        return Table(first.frame, legs, m)


def values(f: Field | str, table: Table) -> np.ndarray:
    """``f``'s value for every row of ``table`` (float array; codes for choice fields, day
    numbers for dates, epoch seconds for the last trade)."""
    if isinstance(f, str):
        f = get(f)
    fr = table.frame
    n = table.n
    if f is None or n == 0:
        return np.full(n, np.nan)
    if f.level == "underlying":
        return fr.u[f.src][table.sym]
    if f.level == "contract":
        return fr.c[f.src][table.legs[0]]
    if f.level == "leg":
        if f.leg > len(table.legs):
            return np.full(n, np.nan)
        return fr.c[f.src][table.legs[f.leg - 1]]
    v = table.m.get(f.key)
    return v if v is not None else np.full(n, np.nan)


def frame_array(f: Field, frame: Frame) -> np.ndarray:
    """The full frame-length array of an underlying / contract / leg field (for masks)."""
    if f.level == "underlying":
        return frame.u[f.src]
    return frame.c[f.src]


# ─────────────────────────────────── display ───────────────────────────────────

def _iso_day(d) -> str | None:
    try:
        if d is None or not np.isfinite(d):
            return None
        return (EPOCH + _dt.timedelta(days=int(d))).isoformat()
    except (TypeError, ValueError, OverflowError):
        return None


def _iso_ts(t) -> str | None:
    if t is None or not np.isfinite(t):
        return None
    return (_dt.datetime(1970, 1, 1) + _dt.timedelta(seconds=float(t))).replace(microsecond=0).isoformat() + "Z"


_ROUND = {"int": 0, "num2": 2, "num4": 4, "pct": 2, "money": 2, "ratio": 2}


def display(f: Field, vals: np.ndarray, frame: Frame) -> list:
    """Row-ready JSON values: rounded floats, None for missing, labels for choices, ISO for
    dates."""
    vals = np.asarray(vals, dtype=float)
    out: list = []
    if f.key == "symbol" or f.base == "symbol":
        syms = frame.symbols
        return [syms[int(v)] if np.isfinite(v) and 0 <= v < len(syms) else None for v in vals.tolist()]
    if f.kind == "choice":
        labels = tuple(c["label"] for c in f.choices)
        for v in vals.tolist():
            out.append(labels[int(v)] if np.isfinite(v) and 0 <= v < len(labels) else None)
        return out
    if f.kind == "bool":
        return [None if not np.isfinite(v) else bool(v) for v in vals.tolist()]
    if f.kind == "date":
        conv = _iso_ts if f.within == "hours" else _iso_day
        return [conv(v) for v in vals.tolist()]
    nd = _ROUND.get(f.fmt, 2)
    for v in vals.tolist():
        if v is None or not np.isfinite(v):
            out.append(None)
        elif nd == 0:
            out.append(int(round(v)))
        else:
            r = round(v, nd)
            out.append(0.0 if r == 0 else r)
    return out


# ─────────────────────────────────── filters ───────────────────────────────────

@dataclass
class Filt:
    field: Field
    op: str
    lo: float | None = None
    hi: float | None = None
    vals: tuple = ()
    noop: bool = False


def _num(v) -> float | None:
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (list, tuple)):
        return _num(v[0]) if v else None
    try:
        x = float(v)
    except (TypeError, ValueError):
        s = str(v).strip().replace(",", "").rstrip("%")
        try:
            x = float(s)
        except ValueError:
            return None
    return x if np.isfinite(x) else None


def _date_num(v, f: Field) -> float | None:
    """An ISO date (or datetime for the last trade) -> the field's number."""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (list, tuple)):
        return _date_num(v[0], f) if v else None
    if f.within == "hours":
        try:
            t = _dt.datetime.fromisoformat(str(v).strip().replace("Z", "+00:00"))
        except ValueError:
            return _num(v)
        if t.tzinfo is not None:
            t = t.astimezone(_dt.timezone.utc).replace(tzinfo=None)
        return (t - _dt.datetime(1970, 1, 1)).total_seconds()
    if isinstance(v, (_dt.date, _dt.datetime)):
        d = v.date() if isinstance(v, _dt.datetime) else v
        return float((d - EPOCH).days)
    try:
        return float((_dt.date.fromisoformat(str(v).strip()[:10]) - EPOCH).days)
    except ValueError:
        return None


def _as_list(v) -> list:
    if v is None:
        return []
    if isinstance(v, (list, tuple, set)):
        return list(v)
    return [v]


def _bool(v) -> bool | None:
    if isinstance(v, (list, tuple)):
        v = v[0] if v else None
    if v is None:
        return True
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    s = str(v).strip().lower()
    if s in ("true", "yes", "1", "y", "on"):
        return True
    if s in ("false", "no", "0", "n", "off"):
        return False
    return None


def _choice_codes(f: Field, vals: list, frame: Frame, warnings: list) -> list:
    base = f.base or f.key
    if base == "symbol":
        out = []
        for v in vals:
            s = str(v).strip().upper()
            code = frame.sym_index.get(s)
            if code is None:
                code = frame.sym_index.get(str(v).strip())
            if code is None:
                warnings.append(f"Symbol '{v}' is not in the market data.")
                out.append(-1)
            else:
                out.append(code)
        return out
    out = []
    for v in vals:
        s = str(v).strip().lower()
        hit = None
        for i, ch in enumerate(f.choices):
            if s in (str(ch["v"]).lower(), str(ch["label"]).lower()):
                hit = i
                break
        if hit is None:
            warnings.append(f"'{v}' is not a value of {f.label} - ignored.")
        else:
            out.append(hit)
    return out


def parse_filters(raw, allowed: set | None, frame: Frame, warnings: list,
                  screen_label: str = "this screen") -> list[Filt]:
    """Payload filters -> ``Filt`` objects. Unknown fields / ops / values become warnings
    and the filter is ignored - never an exception (§5)."""
    out: list[Filt] = []
    if raw is None:
        return out
    if not isinstance(raw, (list, tuple)):
        warnings.append("'filters' must be a list - ignored.")
        return out
    for item in raw:
        if not isinstance(item, dict):
            warnings.append(f"A filter must be an object - ignored ({item!r}).")
            continue
        key = item.get("f")
        f = get(key) if isinstance(key, str) else None
        if f is None:
            warnings.append(f"Unknown filter field '{key}' - ignored.")
            continue
        if allowed is not None and f.key not in allowed:
            warnings.append(f"Filter '{f.label}' does not apply to {screen_label} - ignored.")
            continue
        op = str(item.get("op") or "").strip().lower()
        if op not in f.ops:
            warnings.append(f"Unknown operator '{item.get('op')}' for {f.label} - ignored.")
            continue
        flt = Filt(field=f, op=op)
        lo_raw, hi_raw, v_raw = item.get("lo"), item.get("hi"), item.get("v")
        conv = (lambda x: _date_num(x, f)) if f.kind == "date" else _num
        if op in ("gte", "lte", "between"):
            lo = conv(lo_raw) if lo_raw is not None else None
            hi = conv(hi_raw) if hi_raw is not None else None
            if op == "gte" and lo is None and v_raw is not None:
                lo = conv(v_raw)
            if op == "lte" and hi is None and v_raw is not None:
                hi = conv(v_raw)
            if op == "between" and lo is None and hi is None and isinstance(v_raw, (list, tuple)) and len(v_raw) == 2:
                lo, hi = conv(v_raw[0]), conv(v_raw[1])
            if op == "between" and lo is not None and hi is not None and lo > hi:
                lo, hi = hi, lo
            flt.lo = lo if op != "lte" else None
            flt.hi = hi if op != "gte" else None
            flt.noop = flt.lo is None and flt.hi is None
            if flt.noop and ((lo_raw not in (None, "")) or (hi_raw not in (None, ""))):
                warnings.append(f"{f.label}: could not read the value - ignored.")
        elif op == "eq":
            src = v_raw if v_raw is not None else lo_raw
            if f.kind in ("choice",):
                codes = _choice_codes(f, _as_list(src), frame, warnings)
                flt.vals = tuple(codes[:1])
                flt.noop = not codes
            elif f.kind == "bool":
                b = _bool(src)
                flt.vals = (b,)
                flt.noop = b is None
            else:
                x = conv(src)
                flt.vals = (x,)
                flt.noop = x is None
        elif op == "in":
            vals = _as_list(v_raw)
            if not vals:
                flt.noop = True
            elif f.kind == "date":
                ds = [d for d in (_date_num(v, f) for v in vals) if d is not None]
                if not ds:
                    warnings.append(f"{f.label}: no readable dates - ignored.")
                flt.vals = tuple(ds)
                flt.noop = not ds
            else:
                codes = _choice_codes(f, vals, frame, warnings)
                flt.vals = tuple(codes)
                flt.noop = not codes
        elif op == "is":
            b = _bool(v_raw)
            if b is None:
                warnings.append(f"{f.label}: 'is' needs true or false - ignored.")
            flt.vals = (b,)
            flt.noop = b is None
        elif op == "within":
            x = _num(v_raw if v_raw is not None else hi_raw)
            flt.hi = x
            flt.noop = x is None
        out.append(flt)
    return out


def apply(flt: Filt, arr: np.ndarray, frame: Frame) -> np.ndarray:
    """The boolean mask of ``flt`` over ``arr`` (NaN never matches)."""
    if flt.noop:
        return np.ones(len(arr), dtype=bool)
    op = flt.op
    f = flt.field
    with np.errstate(invalid="ignore"):
        if op == "gte":
            return arr >= flt.lo
        if op == "lte":
            return arr <= flt.hi
        if op == "between":
            m = np.isfinite(arr)
            if flt.lo is not None:
                m &= arr >= flt.lo
            if flt.hi is not None:
                m &= arr <= flt.hi
            return m
        if op == "eq":
            v = flt.vals[0]
            if f.kind == "bool":
                return arr == (1.0 if v else 0.0)
            if f.kind in ("choice", "date"):
                return arr == v
            return np.abs(arr - v) <= 1e-9 * max(1.0, abs(v))
        if op == "in":
            return np.isin(arr, np.asarray(flt.vals, dtype=float))
        if op == "is":
            return arr == (1.0 if flt.vals[0] else 0.0)
        if op == "within":
            n = flt.hi
            if f.within == "hours":
                ref = frame.meta.get("ref_ts")
                if ref is None:
                    ref = _dt.datetime.now(_dt.timezone.utc).timestamp()
                return arr >= ref - n * 3600.0
            d = arr - frame.today_d
            return (d >= 0) & (d <= n)
    return np.ones(len(arr), dtype=bool)


def mask_frame(filts: list[Filt], frame: Frame, size: int) -> np.ndarray:
    """AND of frame-level filters (all underlying-level, or all contract / leg-level)."""
    m = np.ones(size, dtype=bool)
    for flt in filts:
        m &= apply(flt, frame_array(flt.field, frame), frame)
    return m


def mask_table(filts: list[Filt], table: Table) -> np.ndarray:
    m = np.ones(table.n, dtype=bool)
    for flt in filts:
        m &= apply(flt, values(flt.field, table), table.frame)
    return m


@dataclass
class Plan:
    """A parsed payload, filters split by where they apply: ``u`` underlying-level and ``c``
    contract-level (every leg) and ``legs[n]`` leg n's - all three BEFORE pairing - and ``s``
    strategy-level (after the metrics)."""
    u: list
    c: list
    legs: dict
    s: list
    sort: Field
    desc: bool
    max_rows: int

    @classmethod
    def build(cls, filts: list[Filt], sort: Field, desc: bool, max_rows: int) -> "Plan":
        p = cls(u=[], c=[], legs={}, s=[], sort=sort, desc=desc, max_rows=max_rows)
        for flt in filts:
            if flt.noop:
                continue
            lv = flt.field.level
            if lv == "underlying":
                p.u.append(flt)
            elif lv == "contract":
                p.c.append(flt)
            elif lv == "leg":
                p.legs.setdefault(flt.field.leg, []).append(flt)
            else:
                p.s.append(flt)
        return p


def base_mask(frame: Frame, plan: Plan) -> np.ndarray:
    """Contracts whose underlying passes the underlying filters (and has a price) and which
    pass every contract-level filter."""
    mu = mask_frame(plan.u, frame, frame.nu) & np.isfinite(frame.u["spot"])
    m = mu[frame.c["sym"]]
    for flt in plan.c:
        m &= apply(flt, frame_array(flt.field, frame), frame)
    return m


def leg_mask(frame: Frame, plan: Plan, n: int) -> np.ndarray:
    return mask_frame(plan.legs.get(n, []), frame, frame.n)


# ─────────────────────────────────── sorting ───────────────────────────────────

def sort_order(table: Table, f: Field, desc: bool) -> np.ndarray:
    """Stable order of ``table`` by ``f``; missing values always last."""
    key = np.asarray(values(f, table), dtype=float)
    if desc:
        key = -key
    return np.argsort(key, kind="stable")


class TopK:
    """Keeps the best ``cap`` rows of a stream of tables under one sort, and the count of
    everything it saw. Ties keep arrival order (symbol, right, expiry, strike)."""

    def __init__(self, f: Field, desc: bool, cap: int):
        self.f, self.desc, self.cap = f, desc, cap
        self.parts: list[Table] = []
        self.held = 0
        self.total = 0

    def add(self, table: Table) -> None:
        if table is None or table.n == 0:
            return
        self.total += table.n
        self.parts.append(table)
        self.held += table.n
        if self.held > 2 * self.cap + 50_000:
            self._prune()

    def _prune(self) -> None:
        t = Table.concat(self.parts)
        order = sort_order(t, self.f, self.desc)[: self.cap]
        t = t.take(order)
        self.parts, self.held = [t], t.n

    def finish(self, frame: Frame, n_legs: int) -> Table:
        if not self.parts:
            return Table(frame, [np.zeros(0, dtype=np.int64) for _ in range(max(n_legs, 1))])
        t = Table.concat(self.parts)
        order = sort_order(t, self.f, self.desc)[: self.cap]
        return t.take(order)
