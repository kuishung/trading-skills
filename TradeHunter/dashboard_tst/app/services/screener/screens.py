"""The 33 screens (OPTIONS_SCREENER_DESIGN.md §1 / §6): Barchart's Options Screener and its
32 strategy screeners, keyed by Barchart's URL slug.

A ``Screen`` says what the GO TO list shows (label, family, a one-line description in our own
words), which legs the trade has (buy / sell, call / put, which strike role or near / far
term each leg takes in the pairing), its default filters (the cards SET FILTERS opens with),
its Main-view columns and its default sort.

Leg order follows Barchart's tables: credit verticals list the sold leg first, debit
verticals the bought leg; multi-strike structures go by strike, low to high; calendars and
diagonals list the near leg first. Bid / ask columns are replaced by our estimated leg prices
("Sell price (est.)", "Buy price (est.)").
"""
from __future__ import annotations

from dataclasses import dataclass

FAMILIES = ("Options", "Long Options", "Income", "Vertical Spreads", "Protection",
            "Straddles & Strangles", "Horizontal Spreads", "Butterfly Spreads", "Condors")

SINGLE_KINDS = ("options", "long", "covered", "naked", "married")


@dataclass(frozen=True)
class Leg:
    action: str            # "buy" | "sell"
    right: str             # "C" | "P" | "" (the Options Screener's leg is either)
    name: str              # "Short put"
    role: str              # the pairing role (strategies.py)
    qty: int = 1
    term: str = ""         # "near" | "far" for calendars / diagonals

    @property
    def sign(self) -> int:
        return 1 if self.action == "buy" else -1

    def public(self, n: int) -> dict:
        return {"n": n, "action": self.action, "right": self.right, "name": self.name,
                "qty": self.qty, "term": self.term or None}


@dataclass(frozen=True)
class Screen:
    key: str
    label: str
    family: str
    description: str
    bias: str
    kind: str
    legs: tuple
    defaults: tuple
    columns: tuple             # ((field key, label), ...) - the Main view
    sort: tuple                # (field key, "asc" | "desc")
    metrics: tuple = ()        # strategy-level fields this screen computes
    right: str = ""            # single-leg screens: the fixed right ("" = calls and puts)
    credit: bool = False       # opened for a net credit
    stock: bool = False        # the trade holds 100 shares

    @property
    def strategy(self) -> bool:
        return self.kind not in SINGLE_KINDS

    @property
    def horizontal(self) -> bool:
        return self.kind in ("calendar", "diagonal")


# ─────────────────────────────────── default-filter blocks ───────────────────────────────────

def _f(f, op, lo=None, hi=None, v=None) -> dict:
    d = {"f": f, "op": op}
    if lo is not None:
        d["lo"] = lo
    if hi is not None:
        d["hi"] = hi
    if v is not None:
        d["v"] = v
    return d


EXCH = _f("exchange", "in", v=[])                 # an empty card, as Barchart opens it
TREND = _f("trend", "in", v=[])
SECT = _f("sec_type", "in", v=["stock", "etf"])


def _dte(lo, hi):
    return _f("dte", "between", lo, hi)


def _leg(n, f, op, lo=None, hi=None, v=None):
    return _f(f"leg{n}.{f}", op, lo, hi, v)


def _liq(n_legs, vol=10, oi=100, legs=None):
    out = []
    for n in (legs or range(1, n_legs + 1)):
        if vol:
            out.append(_leg(n, "volume", "gte", vol))
        if oi:
            out.append(_leg(n, "oi", "gte", oi))
    return out


# ─────────────────────────────────── metric sets ───────────────────────────────────

_GREEKS = ("net_delta", "net_gamma", "net_theta", "net_vega", "avg_iv_hv")
M_COVERED = ("be", "be_pct", "return_pct", "ann_return", "ptnl_return", "win_prob", "loss_prob",
             "max_profit", "max_loss")
M_NAKED = ("be", "be_pct", "return_pct", "ann_return", "win_prob", "loss_prob", "max_profit",
           "max_loss")
M_MARRIED = ("be", "be_pct", "max_loss", "downside_pct", "win_prob", "loss_prob")
M_VERT = ("width", "be", "be_pct", "max_profit", "max_loss", "max_profit_pct", "risk_reward",
          "win_prob", "loss_prob", "max_profit_prob") + _GREEKS
M_TWO_BE = ("width", "be_hi", "be_hi_pct", "be_lo", "be_lo_pct", "max_profit", "max_loss",
            "max_profit_pct", "risk_reward", "win_prob", "loss_prob", "max_profit_prob") + _GREEKS
M_COLLAR = ("net_credit", "be", "be_pct", "cost_pct", "max_profit", "max_loss", "max_profit_pct",
            "risk_reward", "upside_pct", "downside_pct", "win_prob", "loss_prob",
            "max_profit_prob") + _GREEKS
M_TERM = ("width", "iv_skew") + _GREEKS


# ─────────────────────────────────── column helpers ───────────────────────────────────

def _price_label(legs, n) -> str:
    leg = legs[n - 1]
    same = sum(1 for x in legs if x.action == leg.action)
    if same == 1:
        return ("Buy" if leg.action == "buy" else "Sell") + " price (est.)"
    return f"{leg.name} (est.)"


def _leg_cols(legs, *, strikes=True, prices=True, expiries=False):
    out = []
    for n, leg in enumerate(legs, 1):
        if expiries:
            out.append((f"leg{n}.expiry", f"Exp Leg{n}"))
        if strikes:
            out.append((f"leg{n}.strike", leg.name + (" strike" if not leg.name.endswith("strike") else "")))
        if prices:
            out.append((f"leg{n}.price", _price_label(legs, n)))
    return tuple(out)


SYM_PRICE = (("symbol", "Symbol"), ("stock_price", "Price"))

SINGLE_COLS = (("symbol", "Symbol"), ("stock_price", "Price"), ("option_type", "Type"),
               ("expiry", "Exp Date"), ("strike", "Strike"), ("moneyness", "Moneyness"),
               ("price", "Opt. price (est.)"), ("tp_pct", "%TP"), ("breakeven", "BE"),
               ("breakeven_pct", "%BE"), ("volume", "Volume"), ("oi", "Open Int"),
               ("iv_rank", "IV Rank"), ("iv", "IV"), ("delta", "Delta"),
               ("profit_prob", "Profit Prob"), ("last_trade", "Last Trade"))
LONG_COLS = tuple(c for c in SINGLE_COLS if c[0] != "option_type")


def _covered_cols(ptnl: bool):
    cols = (("symbol", "Symbol"), ("stock_price", "Price"), ("expiry", "Exp Date"), ("strike", "Strike"),
            ("moneyness", "Moneyness"), ("price", "Sell price (est.)"), ("be", "BE"), ("be_pct", "%BE"),
            ("volume", "Volume"), ("oi", "Open Int"), ("iv_rank", "IV Rank"), ("delta", "Delta"),
            ("return_pct", "Return"), ("ann_return", "Ann Rtn"))
    if ptnl:
        cols += (("ptnl_return", "Ptnl Rtn"),)
    return cols + (("win_prob", "Profit Prob"),)


MARRIED_COLS = (("symbol", "Symbol"), ("stock_price", "Price"), ("expiry", "Exp Date"),
                ("strike", "Strike"), ("price", "Buy price (est.)"), ("be", "BE"), ("be_pct", "%BE"),
                ("max_loss", "Max Loss"), ("downside_pct", "Downside"), ("volume", "Volume"),
                ("oi", "Open Int"), ("iv_rank", "IV Rank"), ("delta", "Delta"),
                ("win_prob", "Profit Prob"), ("last_trade", "Last Trade"))


def _vertical_cols(legs, credit):
    net = ("net_credit", "Net Credit") if credit else ("net_debit", "Net Debit")
    prob = ("loss_prob", "Loss Prob") if credit else ("win_prob", "Profit Prob")
    return (SYM_PRICE + (("expiry", "Exp Date"),) + _leg_cols(legs)
            + (net, ("be", "BE"), ("be_pct", "BE%"), ("max_profit", "Max Profit"),
               ("max_loss", "Max Loss"), ("max_profit_pct", "Max Profit%"),
               ("risk_reward", "Risk/Reward"), ("iv_rank", "IV Rank"), prob))


def _vol_cols(legs, credit, straddle):
    net = ("net_credit", "Net Credit") if credit else ("net_debit", "Net Debit")
    probs = ((("loss_prob", "Loss Prob"), ("max_profit_prob", "Max Profit Prob")) if credit
             else (("win_prob", "Profit Prob"),))
    if straddle:
        lc = (("leg1.strike", "Strike"), ("leg1.price", _price_label(legs, 1)),
              ("leg2.price", _price_label(legs, 2)))
    else:
        lc = _leg_cols(legs)
    return (SYM_PRICE + (("expiry", "Exp Date"),) + lc
            + (("be_hi", "BE+"), ("be_hi_pct", "%BE+"), ("be_lo", "BE-"), ("be_lo_pct", "%BE-"), net,
               ("iv_rank", "IV Rank"), ("avg_iv_hv", "IV/HV"), ("net_delta", "Net Delta")) + probs)


def _term_cols(legs, credit, calendar):
    net = ("net_credit", "Net Credit") if credit else ("net_debit", "Net Debit")
    if calendar:
        lc = (("leg1.expiry", "Exp Leg1"), ("leg1.strike", "Strike"), ("leg1.price", _price_label(legs, 1)),
              ("leg2.expiry", "Exp Leg2"), ("leg2.price", _price_label(legs, 2)))
    else:
        lc = (("leg1.expiry", "Exp Leg1"), ("leg1.strike", "Leg1 strike"), ("leg1.price", _price_label(legs, 1)),
              ("leg2.expiry", "Exp Leg2"), ("leg2.strike", "Leg2 strike"), ("leg2.price", _price_label(legs, 2)))
    return (SYM_PRICE + lc + (net, ("leg1.iv", "Leg1 IV"), ("leg2.iv", "Leg2 IV"), ("iv_skew", "IV Skew"),
                               ("iv_rank", "IV Rank"), ("avg_iv_hv", "IV/HV"), ("net_delta", "Net Delta"),
                               ("net_vega", "Net Vega")))


def _wing_cols(legs, credit):
    prob = ("loss_prob", "Loss Prob") if credit else ("win_prob", "Profit Prob")
    net = ("net_credit", "Net Credit") if credit else ("net_debit", "Net Debit")
    return (SYM_PRICE + (("expiry", "Exp Date"),) + _leg_cols(legs)
            + (net, ("be_hi", "BE+"), ("be_lo", "BE-"), ("max_profit", "Max Profit"), ("max_loss", "Max Loss"),
               ("risk_reward", "Risk/Reward"), ("iv_rank", "IV Rank"), prob))


COLLAR_COLS = (SYM_PRICE + (("expiry", "Exp Date"), ("leg1.strike", "Short call"),
                            ("leg1.price", "Sell price (est.)"), ("leg2.strike", "Long put"),
                            ("leg2.price", "Buy price (est.)"), ("be", "BE"), ("net_credit", "Net Cr(Db)"),
                            ("cost_pct", "%Cost"), ("max_profit", "Max Profit"), ("max_loss", "Max Loss"),
                            ("upside_pct", "Upside"), ("downside_pct", "Downside"), ("net_delta", "Delta"),
                            ("win_prob", "Profit Prob")))


# ─────────────────────────────────── the screens ───────────────────────────────────

SCREENS: dict[str, Screen] = {}


def _s(screen: Screen) -> None:
    SCREENS[screen.key] = screen


B, S = "buy", "sell"

# ---- Options ----
_s(Screen(
    "options-screener", "Options Screener", "Options",
    "Every listed call and put across the US options market that passes your filters.",
    "any", "options", (Leg(B, "", "Option", "opt"),),
    (EXCH, _f("option_type", "in", v=[]), _f("expiry", "in", v=[]), _f("dte", "lte", hi=60),
     _f("expiry_type", "in", v=[]), SECT, _f("strike", "between"), _f("volume", "gte", 500),
     _f("oi", "gte", 100), _f("moneyness", "between", -25, 25)),
    SINGLE_COLS, ("symbol", "asc")))

# ---- Long Options ----
_LONG_DEF = (EXCH, _dte(14, 90), SECT, _f("volume", "gte", 100), _f("oi", "gte", 100),
             _f("moneyness", "between", -10, 10))
_s(Screen(
    "long-call", "Long Call", "Long Options",
    "Bullish · buy a call · profit unlimited above the break-even · loss limited to the premium "
    "paid · works when the stock rises enough before expiration.",
    "bullish", "long", (Leg(B, "C", "Long call", "opt"),), _LONG_DEF, LONG_COLS,
    ("profit_prob", "desc"), right="C"))
_s(Screen(
    "long-put", "Long Put", "Long Options",
    "Bearish · buy a put · profit grows as the stock falls below the break-even · loss limited to "
    "the premium paid · works on a sharp enough drop before expiration.",
    "bearish", "long", (Leg(B, "P", "Long put", "opt"),), _LONG_DEF, LONG_COLS,
    ("profit_prob", "desc"), right="P"))

# ---- Income ----
_s(Screen(
    "covered-calls", "Covered Call", "Income",
    "Neutral to mildly bullish · own 100 shares and sell a call · profit capped at the strike plus "
    "the premium · loss is the stock falling, cushioned by the premium · works in a flat or slowly "
    "rising market.",
    "neutral-bullish", "covered", (Leg(S, "C", "Short call", "opt"),),
    (EXCH, SECT, _dte(7, 60), _f("volume", "gte", 100), _f("oi", "gte", 100),
     _f("moneyness", "between", -15, 5)),
    _covered_cols(True), ("ann_return", "desc"), metrics=M_COVERED, right="C", credit=True, stock=True))
_s(Screen(
    "naked-puts", "Naked Put", "Income",
    "Neutral to bullish · sell a put · profit limited to the premium · loss grows if the stock falls "
    "below the break-even · works when the stock holds above the strike.",
    "neutral-bullish", "naked", (Leg(S, "P", "Short put", "opt"),),
    (EXCH, SECT, _dte(7, 60), _f("volume", "gte", 100), _f("oi", "gte", 100),
     _f("moneyness", "between", -20, 0)),
    _covered_cols(False), ("ann_return", "desc"), metrics=M_NAKED, right="P", credit=True))

# ---- Vertical Spreads ----
_VERT_CREDIT_DEF = lambda: (EXCH, TREND, _dte(14, 60), SECT, *_liq(2, legs=(1,)),
                            _leg(1, "moneyness", "between", -25, 0), *_liq(2, legs=(2,)),
                            _leg(1, "otm_prob", "gte", 60), _f("max_profit_pct", "gte", 10))
_VERT_DEBIT_DEF = lambda: (EXCH, TREND, _dte(14, 90), SECT, *_liq(2, legs=(1,)),
                           _leg(1, "moneyness", "between", -10, 10), *_liq(2, legs=(2,)),
                           _f("max_profit_pct", "gte", 50))
_legs = (Leg(B, "C", "Long call", "lo"), Leg(S, "C", "Short call", "hi"))
_s(Screen(
    "bull-call-spread", "Bull Call Spread", "Vertical Spreads",
    "Bullish · buy a call and sell a higher call, same expiration · profit limited to the width "
    "minus the debit · loss limited to the debit · works on a moderate rise.",
    "bullish", "vertical", _legs, _VERT_DEBIT_DEF(), _vertical_cols(_legs, False), ("win_prob", "desc"),
    metrics=("net_debit",) + M_VERT))
_legs = (Leg(S, "C", "Short call", "lo"), Leg(B, "C", "Long call", "hi"))
_s(Screen(
    "bear-call-spread", "Bear Call Spread", "Vertical Spreads",
    "Bearish to neutral · sell a call and buy a higher call · profit is the credit · loss limited "
    "to the width minus the credit · works when the stock stays below the short strike.",
    "bearish", "vertical", _legs, _VERT_CREDIT_DEF(), _vertical_cols(_legs, True), ("loss_prob", "asc"),
    metrics=("net_credit",) + M_VERT, credit=True))
_legs = (Leg(B, "P", "Long put", "hi"), Leg(S, "P", "Short put", "lo"))
_s(Screen(
    "bear-put-spread", "Bear Put Spread", "Vertical Spreads",
    "Bearish · buy a put and sell a lower put · profit limited to the width minus the debit · loss "
    "limited to the debit · works on a moderate fall.",
    "bearish", "vertical", _legs, _VERT_DEBIT_DEF(), _vertical_cols(_legs, False), ("win_prob", "desc"),
    metrics=("net_debit",) + M_VERT))
_legs = (Leg(S, "P", "Short put", "hi"), Leg(B, "P", "Long put", "lo"))
_s(Screen(
    "bull-put-spread", "Bull Put Spread", "Vertical Spreads",
    "Bullish to neutral · sell a put and buy a lower put · profit is the credit · loss limited to "
    "the width minus the credit · works when the stock stays above the short strike.",
    "bullish", "vertical", _legs, _VERT_CREDIT_DEF(), _vertical_cols(_legs, True), ("loss_prob", "asc"),
    metrics=("net_credit",) + M_VERT, credit=True))

# ---- Protection ----
_s(Screen(
    "married-put", "Married Put", "Protection",
    "Bullish with insurance · own 100 shares and buy a put · profit unlimited above the break-even · "
    "loss limited to the cost down to the strike · works when you want the upside but fear a drop.",
    "bullish", "married", (Leg(B, "P", "Long put", "opt"),),
    (EXCH, SECT, _dte(30, 120), _f("volume", "gte", 100), _f("oi", "gte", 100),
     _f("moneyness", "between", -10, 5)),
    MARRIED_COLS, ("downside_pct", "asc"), metrics=M_MARRIED, right="P", stock=True))
_legs = (Leg(S, "C", "Short call", "C"), Leg(B, "P", "Long put", "P"))
_s(Screen(
    "protective-collar", "Protective Collar", "Protection",
    "Mildly bullish, protected · own 100 shares, buy a put below and sell a call above the price · "
    "profit capped at the call strike · loss capped at the put strike · works to protect a gain cheaply.",
    "neutral-bullish", "collar", _legs,
    (EXCH, SECT, _dte(30, 120), _leg(1, "moneyness", "between", -15, 0), _leg(2, "moneyness", "between", -15, 0),
     *_liq(2, vol=0)),
    COLLAR_COLS, ("win_prob", "desc"), metrics=M_COLLAR, stock=True))

# ---- Straddles & Strangles ----
_legs = (Leg(B, "C", "Long call", "C"), Leg(B, "P", "Long put", "P"))
_s(Screen(
    "long-straddle", "Long Straddle", "Straddles & Strangles",
    "A big move either way · buy a call and a put at the same strike · profit unlimited beyond either "
    "break-even · loss limited to the debit · works when a large move comes (earnings, news).",
    "volatile", "straddle", _legs,
    (EXCH, SECT, _dte(14, 90), _leg(1, "moneyness", "between", -5, 5), *_liq(2)),
    _vol_cols(_legs, False, True), ("win_prob", "desc"), metrics=("net_debit",) + M_TWO_BE))
_legs = (Leg(S, "C", "Short call", "C"), Leg(S, "P", "Short put", "P"))
_s(Screen(
    "short-straddle", "Short Straddle", "Straddles & Strangles",
    "Neutral · sell a call and a put at the same strike · profit is the credit, all of it only at the "
    "strike · loss unlimited · works when the stock stays pinned near the strike.",
    "neutral", "straddle", _legs,
    (EXCH, SECT, _dte(14, 60), _leg(1, "moneyness", "between", -5, 5), *_liq(2)),
    _vol_cols(_legs, True, True), ("loss_prob", "asc"), metrics=("net_credit",) + M_TWO_BE, credit=True))
_legs = (Leg(B, "P", "Long put", "P"), Leg(B, "C", "Long call", "C"))
_s(Screen(
    "long-strangle", "Long Strangle", "Straddles & Strangles",
    "A big move either way, for less · buy a lower put and a higher call · profit unlimited beyond the "
    "break-evens · loss limited to the debit · needs a bigger move than a straddle.",
    "volatile", "strangle", _legs,
    (EXCH, SECT, _dte(14, 90), _leg(1, "moneyness", "between", -15, 0), _leg(2, "moneyness", "between", -15, 0),
     *_liq(2)),
    _vol_cols(_legs, False, False), ("win_prob", "desc"), metrics=("net_debit",) + M_TWO_BE))
_legs = (Leg(S, "P", "Short put", "P"), Leg(S, "C", "Short call", "C"))
_s(Screen(
    "short-strangle", "Short Strangle", "Straddles & Strangles",
    "Neutral, range-bound · sell a lower put and a higher call · profit is the credit while the stock "
    "stays between the strikes · loss unlimited · works in a quiet market.",
    "neutral", "strangle", _legs,
    (EXCH, SECT, _dte(14, 60), _leg(1, "delta", "between", -0.30, -0.10), _leg(2, "delta", "between", 0.10, 0.30),
     *_liq(2)),
    _vol_cols(_legs, True, False), ("loss_prob", "asc"), metrics=("net_credit",) + M_TWO_BE, credit=True))

# ---- Horizontal Spreads ----
_TERM_LIQ = lambda: (*_liq(2),)
for _r, _nm in (("C", "Call"), ("P", "Put")):
    _legs = (Leg(S, _r, f"Near {_nm.lower()} (sold)", "near", term="near"),
             Leg(B, _r, f"Far {_nm.lower()} (bought)", "far", term="far"))
    _s(Screen(
        f"long-{_nm.lower()}-calendar", f"Long {_nm} Calendar", "Horizontal Spreads",
        f"Neutral near the strike · sell a near {_nm.lower()} and buy a later one at the same strike · "
        "profit is largest at the strike when the near leg expires · loss limited to the debit · works "
        "when the stock stays put and the later IV holds.",
        "neutral", "calendar", _legs,
        (EXCH, SECT, _leg(1, "dte", "between", 7, 45), _leg(2, "dte", "between", 30, 120),
         _leg(1, "moneyness", "between", -5, 5), *_TERM_LIQ()),
        _term_cols(_legs, False, True), ("iv_skew", "desc"), metrics=("net_debit",) + M_TERM))

_DIAG_DESC = {
    "long-call-diagonal": ("mildly bullish", "Mildly bullish · buy a later, lower call and sell a nearer, "
                           "higher call · profit is largest near the short strike when the near leg expires · "
                           "loss limited to the debit · a covered call with less capital."),
    "short-call-diagonal": ("bearish", "Bearish · sell a later, lower call and buy a nearer, higher call for a "
                            "credit · profit if the stock falls or the later IV drops · loss can be large on a "
                            "rally after the near leg expires."),
    "long-put-diagonal": ("mildly bearish", "Mildly bearish · buy a later, higher put and sell a nearer, lower "
                          "put · profit is largest near the short strike when the near leg expires · loss "
                          "limited to the debit."),
    "short-put-diagonal": ("bullish", "Bullish · sell a later, higher put and buy a nearer, lower put for a "
                           "credit · profit if the stock rises or the later IV drops · loss can be large on a "
                           "drop after the near leg expires."),
}
for _key, _r, _nm, _long in (("long-call-diagonal", "C", "call", True), ("short-call-diagonal", "C", "call", False),
                             ("long-put-diagonal", "P", "put", True), ("short-put-diagonal", "P", "put", False)):
    if _long:
        _legs = (Leg(S, _r, f"Near {_nm} (sold)", "near", term="near"),
                 Leg(B, _r, f"Far {_nm} (bought)", "far", term="far"))
    else:
        _legs = (Leg(B, _r, f"Near {_nm} (bought)", "near", term="near"),
                 Leg(S, _r, f"Far {_nm} (sold)", "far", term="far"))
    _far_delta = (0.60, 0.90) if _r == "C" else (-0.90, -0.60)
    _s(Screen(
        _key, ("Long " if _long else "Short ") + _nm.title() + " Diagonal", "Horizontal Spreads",
        _DIAG_DESC[_key][1], _DIAG_DESC[_key][0], "diagonal", _legs,
        (EXCH, SECT, _leg(1, "dte", "between", 7, 45), _leg(2, "dte", "between", 30, 180),
         _leg(1, "moneyness", "between", -15, 0), _leg(2, "delta", "between", *_far_delta), *_TERM_LIQ()),
        _term_cols(_legs, not _long, False), ("iv_skew", "desc" if _long else "asc"),
        metrics=(("net_debit",) if _long else ("net_credit",)) + M_TERM, credit=not _long))

# ---- Butterfly Spreads ----
_WING_LIQ = lambda n: tuple(_leg(i, "oi", "gte", 100) for i in range(1, n + 1))
for _r, _nm in (("C", "Call"), ("P", "Put")):
    _lr = _nm.lower()
    for _long in (True, False):
        o, i = (B, S) if _long else (S, B)
        _legs = (Leg(o, _r, f"Lower {_lr}", "k1"), Leg(i, _r, f"Middle {_lr}s (x2)", "k2", qty=2),
                 Leg(o, _r, f"Upper {_lr}", "k3"))
        _defs = (EXCH, SECT, _dte(14, 60), _leg(2, "moneyness", "between", -5, 5), *_WING_LIQ(3))
        if not _long:
            _defs += (_f("max_profit_pct", "gte", 10),)
        _s(Screen(
            f"{'long' if _long else 'short'}-{_lr}-butterfly", f"{'Long' if _long else 'Short'} {_nm} Butterfly",
            "Butterfly Spreads",
            (f"Neutral, pinned · buy 1 lower {_lr}, sell 2 middle {_lr}s, buy 1 upper {_lr} (equal wings) · "
             "profit is largest at the middle strike · loss limited to the debit · works when the stock ends "
             "near the middle.") if _long else
            (f"A move away from the middle · sell 1 lower {_lr}, buy 2 middle {_lr}s, sell 1 upper {_lr} "
             "(equal wings) · profit is the credit beyond the wings · loss limited to the width minus the "
             "credit."),
            "neutral" if _long else "volatile", "butterfly", _legs, _defs, _wing_cols(_legs, not _long),
            ("win_prob", "desc") if _long else ("loss_prob", "asc"),
            metrics=(("net_debit",) if _long else ("net_credit",)) + M_TWO_BE, credit=not _long))

for _long in (True, False):
    o, i = (S, B) if _long else (B, S)     # wings / body
    _legs = (Leg(o, "P", "Lower put", "p1"), Leg(i, "P", "Middle put", "p2"),
             Leg(i, "C", "Middle call", "c2"), Leg(o, "C", "Upper call", "c3"))
    _defs = (EXCH, SECT, _dte(14, 60), _leg(2, "moneyness", "between", -5, 5), *_WING_LIQ(4))
    if not _long:
        _defs += (_f("max_profit_pct", "gte", 10),)
    _s(Screen(
        f"{'long' if _long else 'short'}-iron-butterfly", f"{'Long' if _long else 'Short'} Iron Butterfly",
        "Butterfly Spreads",
        ("A big move either way · buy the put and the call at the middle strike, sell a lower put and an "
         "upper call · profit limited to the width minus the debit beyond the wings · loss limited to the "
         "debit.") if _long else
        ("Neutral, pinned · sell the put and the call at the middle strike, buy a lower put and an upper "
         "call · profit is the credit, all of it only at the middle · loss limited to the width minus the "
         "credit."),
        "volatile" if _long else "neutral", "iron_butterfly", _legs, _defs, _wing_cols(_legs, not _long),
        ("win_prob", "desc") if _long else ("loss_prob", "asc"),
        metrics=(("net_debit",) if _long else ("net_credit",)) + M_TWO_BE, credit=not _long))

# ---- Condors ----
for _r, _nm in (("C", "Call"), ("P", "Put")):
    _lr = _nm.lower()
    for _long in (True, False):
        o, i = (B, S) if _long else (S, B)
        _legs = (Leg(o, _r, f"Lowest {_lr}", "k1"), Leg(i, _r, f"Lower middle {_lr}", "k2"),
                 Leg(i, _r, f"Upper middle {_lr}", "k3"), Leg(o, _r, f"Highest {_lr}", "k4"))
        _defs = (EXCH, SECT, _dte(14, 60), _leg(2, "moneyness", "between", -10, 10),
                 _leg(3, "moneyness", "between", -10, 10), *_WING_LIQ(4))
        if not _long:
            _defs += (_f("max_profit_pct", "gte", 10),)
        _s(Screen(
            f"{'long' if _long else 'short'}-{_lr}-condor", f"{'Long' if _long else 'Short'} {_nm} Condor",
            "Condors",
            (f"Neutral, range-bound · buy the lowest {_lr}, sell the two middle {_lr}s, buy the highest "
             "(equal outer gaps) · profit limited, earned between the middle strikes · loss limited to the "
             "debit.") if _long else
            (f"A breakout either way · sell the lowest {_lr}, buy the two middle {_lr}s, sell the highest · "
             "profit is the credit beyond the outer strikes · loss limited to the width minus the credit."),
            "neutral" if _long else "volatile", "condor", _legs, _defs, _wing_cols(_legs, not _long),
            ("win_prob", "desc") if _long else ("loss_prob", "asc"),
            metrics=(("net_debit",) if _long else ("net_credit",)) + M_TWO_BE, credit=not _long))

for _long in (True, False):
    o, i = (S, B) if _long else (B, S)     # outer wings / inner legs
    _legs = (Leg(o, "P", "Lower put", "p1"), Leg(i, "P", "Inner put", "p2"),
             Leg(i, "C", "Inner call", "c3"), Leg(o, "C", "Upper call", "c4"))
    _defs = (EXCH, SECT, _dte(14, 60), _leg(2, "delta", "between", -0.30, -0.10),
             _leg(3, "delta", "between", 0.10, 0.30), *_WING_LIQ(4))
    if not _long:
        _defs += (_f("max_profit_pct", "gte", 10),)
    _s(Screen(
        f"{'long' if _long else 'short'}-iron-condor", f"{'Long' if _long else 'Short'} Iron Condor", "Condors",
        ("A breakout either way · buy a put and a call near the money, sell a further put and call · "
         "profit limited to the width minus the debit beyond the wings · loss limited to the debit.")
        if _long else
        ("Neutral, range-bound · sell an out-of-the-money put and call, buy further wings for protection · "
         "profit is the credit while the stock stays between the short strikes · loss limited to the width "
         "minus the credit."),
        "volatile" if _long else "neutral", "iron_condor", _legs, _defs, _wing_cols(_legs, not _long),
        ("win_prob", "desc") if _long else ("loss_prob", "asc"),
        metrics=(("net_debit",) if _long else ("net_credit",)) + M_TWO_BE, credit=not _long))

# Barchart's GO TO order
ORDER = ("options-screener", "long-call", "long-put", "covered-calls", "naked-puts", "bull-call-spread",
         "bear-call-spread", "bear-put-spread", "bull-put-spread", "married-put", "protective-collar",
         "long-straddle", "short-straddle", "long-strangle", "short-strangle", "long-call-calendar",
         "long-put-calendar", "long-call-diagonal", "short-call-diagonal", "long-put-diagonal",
         "short-put-diagonal", "long-call-butterfly", "short-call-butterfly", "long-put-butterfly",
         "short-put-butterfly", "long-iron-butterfly", "short-iron-butterfly", "long-call-condor",
         "short-call-condor", "long-put-condor", "short-put-condor", "long-iron-condor", "short-iron-condor")
SCREENS = {k: SCREENS[k] for k in ORDER}
assert len(SCREENS) == 33
