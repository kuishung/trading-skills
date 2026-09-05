"""ORM models.

Auth model: **Google OAuth + admin approval**. Google authenticates the
user (no passwords stored); a new user lands in ``status='pending'`` and
cannot access member areas until an admin approves them.

Phase 1 (now): ``User`` + status/roles.
Phases 2-3 (stubbed here so the schema can evolve): ``MATPLevel`` (the
quarterly target board), ``Setup`` (a collaborative trade study with the
agreed entry/SL/PT), ``Comment`` (discussion).
"""
from __future__ import annotations

import datetime as _dt

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship

from .db import Base

# status values
PENDING = "pending"
APPROVED = "approved"
DISABLED = "disabled"

# role values (administrator > moderator > member)
ROLE_ADMIN = "admin"
ROLE_MODERATOR = "moderator"
ROLE_MEMBER = "member"
ROLES = (ROLE_ADMIN, ROLE_MODERATOR, ROLE_MEMBER)
ROLE_LABELS = {
    ROLE_ADMIN: "Administrator",
    ROLE_MODERATOR: "Moderator",
    ROLE_MEMBER: "Member",
}


def _utcnow() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    email = Column(String(255), unique=True, nullable=False, index=True)
    # password mode (Path B): PBKDF2 hash. google mode (Path A): OIDC subject.
    # A user has one or the other depending on TST_AUTH_MODE.
    password_hash = Column(String(255), nullable=True)
    google_sub = Column(String(64), unique=True, nullable=True, index=True)  # OIDC subject
    display_name = Column(String(120), nullable=False, default="")
    picture = Column(String(512), nullable=True)
    role = Column(String(20), nullable=False, default="member")   # member | admin
    status = Column(String(20), nullable=False, default=PENDING)  # pending | approved | disabled
    # per-user menu access — JSON list of menu keys (app/menus.py). NULL = all
    # (back-compat / default-open); admin sets an explicit subset to restrict.
    menu_access = Column(JSON, nullable=True)
    # per-user UI preferences (JSON blob), e.g. {"rrg_sectors": ["XLK","XLF"]}.
    prefs = Column(JSON, nullable=True)
    created_at = Column(DateTime, default=_utcnow)
    approved_at = Column(DateTime, nullable=True)

    @property
    def is_admin(self) -> bool:
        return self.role == ROLE_ADMIN

    @property
    def is_moderator(self) -> bool:
        return self.role == ROLE_MODERATOR

    @property
    def can_moderate(self) -> bool:
        """Moderators and admins can moderate content."""
        return self.role in (ROLE_ADMIN, ROLE_MODERATOR)

    @property
    def role_label(self) -> str:
        return ROLE_LABELS.get(self.role, self.role)

    @property
    def is_approved(self) -> bool:
        return self.status == APPROVED

    @property
    def is_pending(self) -> bool:
        return self.status == PENDING


class MATPLevel(Base):
    """Quarterly Median Analyst Target Price / Max Buy Price per ticker.
    Populated by the resources/MATP pipeline (Phase 2)."""

    __tablename__ = "matp_levels"

    id = Column(Integer, primary_key=True)
    symbol = Column(String(20), nullable=False, index=True)
    exchange = Column(String(20), nullable=True)
    last_earnings_date = Column(String(20), nullable=True)
    matp = Column(Float, nullable=True)
    mbp = Column(Float, nullable=True)
    n_targets = Column(Integer, nullable=True)  # post-earnings count behind the median
    trend = Column(String(20), nullable=True)  # from resources.trend_state
    # ---- universe membership / Finviz-filter drift tracking ----
    # The Finviz screen is dynamic: each run some names fall out and others
    # qualify. We never delete -- a ticker that leaves the screen is marked
    # 'dropped' (its MATP history is retained) so the board can default to
    # current candidates while keeping the time series.
    status = Column(String(12), nullable=False, default="active")  # active | dropped
    filter_id = Column(Integer, ForeignKey("finviz_filters.id"), nullable=True, index=True)
    last_seen_at = Column(DateTime, nullable=True)  # last run that included this ticker
    # ---- actionable bounce signal (daily_bounce_alert logic, pushed by agent) ----
    signal = Column(String(12), nullable=True)        # HOT | WARM | WATCHING
    signal_entry = Column(Float, nullable=True)       # last close
    signal_stop = Column(Float, nullable=True)        # last low
    signal_target = Column(Float, nullable=True)      # usually = MATP
    signal_rr = Column(Float, nullable=True)          # reward:risk to target
    as_of = Column(DateTime, default=_utcnow)  # quarterly refresh stamp


class MATPHistory(Base):
    """Append-only MATP/MBP observations over time, so the board can show how a
    ticker's analyst target evolved. De-duped on write: a new row is appended
    only when the MATP value actually changes (analysts update infrequently)."""

    __tablename__ = "matp_history"

    id = Column(Integer, primary_key=True)
    symbol = Column(String(20), nullable=False, index=True)
    matp = Column(Float, nullable=False)  # the median (the headline number)
    mbp = Column(Float, nullable=True)
    last_earnings_date = Column(String(20), nullable=True)
    n_targets = Column(Integer, nullable=True)
    # distribution summary of the post-earnings targets behind the median (B)
    target_high = Column(Float, nullable=True)
    target_low = Column(Float, nullable=True)
    target_mean = Column(Float, nullable=True)
    source = Column(String(40), nullable=True)  # e.g. "nous_hermes"
    as_of = Column(DateTime, default=_utcnow, index=True)


class MATPTarget(Base):
    """One analyst price target (the evidence behind a MATP). Stored once per
    unique (symbol, brokerage, target_date, target_price) -- re-pushing the same
    list each run does NOT duplicate. `included` (post-earnings?) is computed on
    display from target_date vs the current earnings date, so it never goes
    stale."""

    __tablename__ = "matp_targets"
    __table_args__ = (
        UniqueConstraint(
            "symbol", "brokerage", "target_date", "target_price", name="uq_matp_target"
        ),
    )

    id = Column(Integer, primary_key=True)
    symbol = Column(String(20), nullable=False, index=True)
    brokerage = Column(String(120), nullable=True)
    target_price = Column(Float, nullable=False)
    target_date = Column(String(20), nullable=True)  # YYYY-MM-DD, the issue date
    as_of = Column(DateTime, default=_utcnow)  # when first recorded


class MATPRefreshRequest(Base):
    """A collaborator-triggered ad-hoc MATP refresh. TradeHunter is LLM-free and
    can't fetch, and the Nous Hermes agent only calls outward -- so a click here
    just enqueues a 'pending' row. The agent polls /api/refresh-queue, runs the
    work, pushes results via /api/matp, and marks the row done. Near-real-time,
    not synchronous. Moderators/admins only (enforced in the route)."""

    __tablename__ = "matp_refresh_requests"

    id = Column(Integer, primary_key=True)
    scope = Column(String(10), nullable=False)  # 'ticker' | 'filter'
    symbol = Column(String(20), nullable=True, index=True)        # ticker scope
    filter_id = Column(Integer, ForeignKey("finviz_filters.id"), nullable=True)  # filter scope
    status = Column(String(12), nullable=False, default="pending", index=True)  # pending|running|done|failed
    note = Column(Text, nullable=True)  # agent's result / error message
    # live progress (agent reports as it works the universe; null until it starts)
    progress_done = Column(Integer, nullable=True)   # tickers processed so far
    progress_total = Column(Integer, nullable=True)  # tickers in this run
    requested_by = Column(Integer, ForeignKey("users.id"))
    created_at = Column(DateTime, default=_utcnow)
    claimed_at = Column(DateTime, nullable=True)     # when the agent started it
    completed_at = Column(DateTime, nullable=True)   # done / failed stamp

    requester = relationship("User")
    filter = relationship("FinvizFilter")


class Setup(Base):
    """A collaborative trade study: the social core of the platform.
    Members converge on entry / stop-loss / profit-target."""

    __tablename__ = "setups"

    id = Column(Integer, primary_key=True)
    symbol = Column(String(20), nullable=False, index=True)
    title = Column(String(200), nullable=False)
    pattern = Column(String(80), nullable=True)        # from resources.patterns
    trend_state = Column(String(20), nullable=True)    # from resources.trend_state
    rationale = Column(Text, nullable=True)
    # The collaboratively-agreed trade decision
    entry = Column(Float, nullable=True)
    stop_loss = Column(Float, nullable=True)
    profit_target = Column(Float, nullable=True)
    # curator's marked horizontal levels (determined during the study)
    support = Column(Float, nullable=True)
    resistance = Column(Float, nullable=True)
    # per-study Discord thread (bot-created) whose messages we show on the page
    discord_thread_id = Column(String(40), nullable=True)
    status = Column(String(20), nullable=False, default="draft")  # draft|discussing|agreed|closed
    created_by = Column(Integer, ForeignKey("users.id"))
    created_at = Column(DateTime, default=_utcnow)

    author = relationship("User")
    comments = relationship(
        "Comment", back_populates="setup", cascade="all, delete-orphan"
    )


class Comment(Base):
    __tablename__ = "comments"

    id = Column(Integer, primary_key=True)
    setup_id = Column(Integer, ForeignKey("setups.id"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    body = Column(Text, nullable=False)
    created_at = Column(DateTime, default=_utcnow)

    setup = relationship("Setup", back_populates="comments")
    user = relationship("User")


class FinvizFilter(Base):
    """A saved Finviz screener: URL + description + active flag. Moderators
    manage the list; the tickers from *active* filters form the shared
    universe everyone studies. New table -> create_all adds it safely."""

    __tablename__ = "finviz_filters"

    id = Column(Integer, primary_key=True)
    description = Column(String(200), nullable=False)
    url = Column(Text, nullable=False)
    is_active = Column(Boolean, nullable=False, default=True)
    # scheduled MATP run: the agent's poll cron runs filters that are *due*.
    run_interval = Column(String(12), nullable=False, default="off")  # off|daily|weekly|monthly|quarterly
    last_run_at = Column(DateTime, nullable=True)
    next_run_at = Column(DateTime, nullable=True, index=True)
    created_by = Column(Integer, ForeignKey("users.id"))
    created_at = Column(DateTime, default=_utcnow)

    author = relationship("User")


class SelectiveSchedule(Base):
    """Singleton schedule for the 'Selective tickers' set — the ad-hoc MATPLevel
    rows with filter_id NULL (names keyed in by hand). Mirrors FinvizFilter's
    scheduling fields so the agent's poll can refresh the manual tickers on their
    OWN cadence, independent of whether any Finviz filter is due. Exactly one row
    (id=1; see get_selective_schedule). New table -> create_all adds it safely."""

    __tablename__ = "selective_schedule"

    id = Column(Integer, primary_key=True)
    run_interval = Column(String(12), nullable=False, default="off")  # off|daily|weekly|monthly|quarterly
    last_run_at = Column(DateTime, nullable=True)
    next_run_at = Column(DateTime, nullable=True, index=True)


# interval -> days; the agent runs a filter when next_run_at <= now.
RUN_INTERVALS = {"off": None, "daily": 1, "weekly": 7, "monthly": 30, "quarterly": 91}


def get_selective_schedule(db):
    """Fetch (or lazily create) the single Selective-tickers schedule row."""
    row = db.get(SelectiveSchedule, 1)
    if row is None:
        row = SelectiveSchedule(id=1, run_interval="off")
        db.add(row)
        db.commit()
    return row


class AgentHeartbeat(Base):
    """Liveness + cron self-report from the outbound-only Nous Hermes agent.

    The agent dials OUT (it polls /api/refresh-queue + /api/due-filters and
    pushes MATP); TradeHunter can't reach into the Linux box. So on each poll
    the agent POSTs a heartbeat here — its version, the literal ``crontab -l``
    lines it's running, and its own clock — and the dashboard's /agent page
    shows online/stale + the crons. One row per agent name (upserted)."""

    __tablename__ = "agent_heartbeats"

    id = Column(Integer, primary_key=True)
    agent = Column(String(60), unique=True, nullable=False, index=True)  # e.g. "nous_hermes"
    version = Column(String(40), nullable=True)   # agent / skill version string
    crons = Column(Text, nullable=True)           # raw `hermes cron list` text (fallback)
    # structured cron jobs incl. the FULL prompt each one runs, so the /agent
    # page can show what every cron actually does (not the truncated Name):
    #   [{"id","schedule","skills","prompt","next_run","active"}]
    cron_jobs = Column(JSON, nullable=True)
    host = Column(String(120), nullable=True)     # optional hostname
    # MEASURED health from heartbeat.sh (added 2026-08-16):
    #   {agent_ok, agent_error, agent_version, gateway, disk_pct, disk_free, disk_path}
    # A beat proves the BOX is up; this proves the AGENT can actually run. The
    # 2026-08-06 outage stayed invisible for ten days precisely because the beat
    # kept landing while the agent was dead. None = an older heartbeat.sh that
    # doesn't report health yet -> the UI shows "health not reported", never a
    # false green.
    health = Column(JSON, nullable=True)
    polled_at = Column(DateTime, nullable=True)   # agent's own clock at send
    received_at = Column(DateTime, default=_utcnow)  # server clock on receipt


class IngestHealth(Base):
    """Latest parquet-ingest health report, pushed by the Hermes-side reporter
    cron (POST /api/ingest/health). The dashboard doesn't read the bars store
    over the network — Hermes prepares the report and reports in, like the agent
    heartbeat. One row (upserted by host). ``report`` holds the raw payload:
    {timeframes:[{tf,symbols,mb,newest_epoch}], generated_epoch, log_tail:[...]}.
    """

    __tablename__ = "ingest_health"

    id = Column(Integer, primary_key=True)
    host = Column(String(120), unique=True, nullable=False, index=True)
    report = Column(JSON, nullable=True)
    received_at = Column(DateTime, default=_utcnow)  # server clock on receipt


class EdgarIngestHealth(Base):
    """Latest EDGAR earnings-filing ingest report, pushed by the AI-Hermes
    reporter (POST /api/ingest/edgar). The EDGAR corpus lives on AI-Hermes
    (the Windows file server, 192.168.1.162) — the web app can't read it over
    the network, so AI-Hermes scans the corpus + looks up each ticker's
    earnings dates and reports in, exactly like the parquet IngestHealth above.
    One row (upserted by host). ``report`` holds the raw payload:
    {host, generated_epoch, root, tickers:[{ticker, latest_period,
    newest_epoch, html, md, stub_md, last_earnings, next_earnings}],
    log_tail:[...]}.
    """

    __tablename__ = "edgar_ingest_health"

    id = Column(Integer, primary_key=True)
    host = Column(String(120), unique=True, nullable=False, index=True)
    report = Column(JSON, nullable=True)
    received_at = Column(DateTime, default=_utcnow)  # server clock on receipt


class Feedback(Base):
    """Development feedback board -- collaborators comment on the build as it
    goes (not tied to a specific setup). The lightweight 'react to each part
    as it ships' surface."""

    __tablename__ = "feedback"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    topic = Column(String(120), nullable=True)  # optional free-text label
    body = Column(Text, nullable=False)
    created_at = Column(DateTime, default=_utcnow)

    user = relationship("User")


# ---------------------------------------------------------------------------
# Research — member co-designs a research topic with the LLM (DeepSeek, called
# DIRECTLY by the dashboard for the planning chat), records the agreed plan as
# Markdown, schedules it on a per-topic hermes cron, and the Nous agent runs it
# OUTBOUND-ONLY: it polls /api/research/due, executes with its corpus + cifs,
# writes the output md to AI-Hermes, and POSTs the result back. See
# RESEARCH_DESIGN.md. Portable column types only (Postgres-upgradeable).
# ---------------------------------------------------------------------------

# topic kinds
RESEARCH_MACRO = "macro"
RESEARCH_COMPANY = "company"

# topic status lifecycle
RT_DRAFT = "draft"          # being chatted/planned
RT_PLANNED = "planned"      # a PLAN.md has been agreed
RT_SCHEDULED = "scheduled"  # a cron is registered
RT_ARCHIVED = "archived"

# run status
RUN_QUEUED = "queued"
RUN_RUNNING = "running"
RUN_OK = "ok"
RUN_ERROR = "error"


class ResearchTopic(Base):
    """A unit of research the user co-designs with the LLM and schedules."""

    __tablename__ = "research_topics"

    id = Column(Integer, primary_key=True)
    owner_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    title = Column(String(200), nullable=False)
    kind = Column(String(12), nullable=False, default=RESEARCH_COMPANY)  # macro | company
    subject = Column(String(120), nullable=True)   # ticker (company) or theme (macro)
    status = Column(String(12), nullable=False, default=RT_DRAFT)
    # which inputs the agent should pull when running (JSON list: edgar/matp/bars/web)
    sources = Column(JSON, nullable=True)
    # the agreed research plan, Markdown. DB is the source of truth; the agent
    # renders this to PLAN.md in the corpus when it runs.
    plan_md = Column(Text, nullable=True)
    # cron expression for the per-topic hermes cron (null = not scheduled)
    schedule_cron = Column(String(120), nullable=True)
    enabled = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime, default=_utcnow)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow)

    owner = relationship("User")
    messages = relationship(
        "ResearchMessage", back_populates="topic",
        cascade="all, delete-orphan", order_by="ResearchMessage.seq",
    )
    runs = relationship(
        "ResearchRun", back_populates="topic",
        cascade="all, delete-orphan", order_by="ResearchRun.id.desc()",
    )


class ResearchMessage(Base):
    """One turn of the planning conversation. Rendered into PLAN.md context."""

    __tablename__ = "research_messages"

    id = Column(Integer, primary_key=True)
    topic_id = Column(Integer, ForeignKey("research_topics.id"), nullable=False, index=True)
    seq = Column(Integer, nullable=False, default=0)   # order within the topic
    role = Column(String(12), nullable=False)          # user | assistant | system
    content = Column(Text, nullable=False)
    created_at = Column(DateTime, default=_utcnow)

    topic = relationship("ResearchTopic", back_populates="messages")


class ResearchRun(Base):
    """One execution of a topic's plan by the Nous agent (outbound-only)."""

    __tablename__ = "research_runs"

    id = Column(Integer, primary_key=True)
    topic_id = Column(Integer, ForeignKey("research_topics.id"), nullable=False, index=True)
    status = Column(String(12), nullable=False, default=RUN_QUEUED)
    trigger = Column(String(12), nullable=False, default="manual")  # manual | cron
    queued_at = Column(DateTime, default=_utcnow)
    started_at = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, nullable=True)
    output_md_path = Column(String(512), nullable=True)  # path in the AI-Hermes corpus
    summary = Column(Text, nullable=True)
    tokens = Column(Integer, nullable=True)
    error = Column(Text, nullable=True)

    topic = relationship("ResearchTopic", back_populates="runs")


# --------------------------------------------------------------------------
# Pattern Trainer — teach a chart pattern by example, generate a detector.
# (see dashboard_tst/PATTERN_TRAINER_DESIGN.md)
# --------------------------------------------------------------------------
PT_LEARNING = "learning"   # being taught via chat
PT_READY = "ready"         # pattern.md + detect.py generated
PT_ARCHIVED = "archived"


class Pattern(Base):
    """A user-taught chart pattern. The teaching transcript (PatternLesson) is
    distilled into a Markdown spec (`pattern_md`) + a Python detector
    (`detect_py`); both are also committed to the repo under
    strategy/patterns/<slug>/. The DB is the source of truth; the files are a
    rendered projection (per the data-handling rule)."""

    __tablename__ = "patterns"

    id = Column(Integer, primary_key=True)
    owner_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    name = Column(String(120), nullable=False)
    slug = Column(String(120), nullable=False, index=True)  # filesystem-safe id
    description = Column(String(400), nullable=True)
    status = Column(String(12), nullable=False, default=PT_LEARNING)
    # last chart the user was teaching on (so the page reopens where they left)
    chart_symbol = Column(String(20), nullable=True)
    chart_timeframe = Column(String(8), nullable=True)       # daily | 3min | 1min
    pattern_md = Column(Text, nullable=True)                 # learned spec
    detect_py = Column(Text, nullable=True)                  # generated detector
    md_path = Column(String(512), nullable=True)             # committed repo path
    script_path = Column(String(512), nullable=True)
    # ---- calibration state (the D4 loop, surfaced in the trainer readiness card)
    # `detector_thresholds` is the fitted thresholds dict (merge-compatible with the
    # detector's SEED_THRESHOLDS); the detector runs with it once calibrated. The
    # rest record how good/recent that calibration is so the page can gate Promote.
    detector_thresholds = Column(JSON, nullable=True)
    detector_version = Column(String(20), nullable=True)     # __version__ at calibration
    calib_pass_rate = Column(Float, nullable=True)           # last validation-suite pass (0..1)
    calib_at = Column(DateTime, nullable=True)               # when last calibrated
    created_at = Column(DateTime, default=_utcnow)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow)

    owner = relationship("User")
    lessons = relationship(
        "PatternLesson", back_populates="pattern",
        cascade="all, delete-orphan", order_by="PatternLesson.seq",
    )
    examples = relationship(
        "PatternExample", back_populates="pattern",
        cascade="all, delete-orphan", order_by="PatternExample.created_at",
    )


class PatternExample(Base):
    """A saved teaching example for a pattern: a chart region the user DREW (or
    corrected) on the chart by direct manipulation — never typed. The canonical
    labelling UX is drag-to-label (DETECTOR_DESIGN.md): the user drags the
    resistance + support trendlines on the chart; the geometry is stored here and
    the numeric features are DERIVED from it (the user never types thresholds).

    `kind` is the label polarity (positive = is the pattern; negative = a rejected
    near-miss — both are needed to calibrate). `geometry` is the drawn shape
    (trendline endpoints) the calibrator reads features from."""

    __tablename__ = "pattern_examples"

    id = Column(Integer, primary_key=True)
    pattern_id = Column(Integer, ForeignKey("patterns.id"), nullable=False, index=True)
    symbol = Column(String(20), nullable=False)
    timeframe = Column(String(8), nullable=False)            # daily | 5min | 3min | 1min
    start_t = Column(String(32), nullable=False)             # ISO window bound
    end_t = Column(String(32), nullable=False)
    n_bars = Column(Integer, nullable=True)
    # label polarity — the calibration bridge (positives + rejected near-misses)
    kind = Column(String(10), nullable=False, default="positive")  # positive | negative
    # the drawn shape: {"resistance": {t0,p0,t1,p1}, "support": {t0,p0,t1,p1}}.
    # Features (slope/R²/touches/contraction) are computed FROM this, not typed.
    geometry = Column(JSON, nullable=True)
    label = Column(String(120), nullable=True)               # optional user label
    note = Column(Text, nullable=True)
    created_at = Column(DateTime, default=_utcnow)

    pattern = relationship("Pattern", back_populates="examples")


class PatternLesson(Base):
    """One turn of the teaching conversation. `marked` records which chart
    region (symbol/timeframe/time-range) the user was pointing at for that
    turn, so the bars can be re-loaded and injected as context."""

    __tablename__ = "pattern_lessons"

    id = Column(Integer, primary_key=True)
    pattern_id = Column(Integer, ForeignKey("patterns.id"), nullable=False, index=True)
    seq = Column(Integer, nullable=False, default=0)
    role = Column(String(12), nullable=False)               # user | assistant | system
    content = Column(Text, nullable=False)
    marked = Column(JSON, nullable=True)                    # {symbol, timeframe, start, end, n}
    created_at = Column(DateTime, default=_utcnow)

    pattern = relationship("Pattern", back_populates="lessons")


# ── Company Analysis (per-ticker dossier) ──────────────────────────────────────
# See dashboard_tst/COMPANY_ANALYSIS_DESIGN.md. One row per (symbol, section):
# a fixed-section, sourced, per-ticker analysis. Content is agent-generated (from
# EDGAR + industry knowledge) or moderator-edited; every section carries provenance.
CA_BUSINESS_MODEL = "business_model"
CA_SEGMENT = "segment"
CA_COMPETITIVE = "competitive"
CA_SUPPLIERS = "suppliers"
CA_KPI = "kpi"
CA_SECTIONS = (CA_BUSINESS_MODEL, CA_SEGMENT, CA_COMPETITIVE, CA_SUPPLIERS, CA_KPI)
CA_SECTION_LABELS = {
    CA_BUSINESS_MODEL: "Business Model",
    CA_SEGMENT: "Business Segment",
    CA_COMPETITIVE: "Competitive Analysis",
    CA_SUPPLIERS: "Suppliers",
    CA_KPI: "Key Metrics (KPI)",
}


class CompanyAnalysis(Base):
    __tablename__ = "company_analysis"

    id = Column(Integer, primary_key=True)
    symbol = Column(String(20), nullable=False, index=True)
    section = Column(String(32), nullable=False)   # one of CA_SECTIONS
    body = Column(Text, nullable=True)             # long-form prose (qualitative sections)
    content = Column(JSON, nullable=True)          # structured payload (tables/lists/tiers/scorecard)
    sources = Column(JSON, nullable=True)          # [{title, url, accession, kind}]
    source_kind = Column(String(16), nullable=False, default="manual")  # manual | agent | feed
    confidence = Column(String(16), nullable=True)  # high | medium | low (esp. inferred tier-2)
    industry = Column(String(80), nullable=True)   # classified industry (drives templates/peers)
    as_of = Column(DateTime, default=_utcnow)
    updated_by = Column(String(120), nullable=True)

    __table_args__ = (
        UniqueConstraint("symbol", "section", name="uq_company_analysis_symbol_section"),
    )


# Macro board sections — the fixed top-down taxonomy behind /macro. Ordered;
# the left rail renders them in this order and the keys are the URL segments.
MACRO_SECTIONS = (
    "policy_rates",
    "growth_inflation",
    "internals",
    "cross_asset",
    "global_geo",
    "liquidity",
)
MACRO_SECTION_LABELS = {
    "policy_rates": "Monetary policy & rates",
    "growth_inflation": "Growth & inflation data",
    "internals": "Market internals",
    "cross_asset": "Cross-asset signals",
    "global_geo": "Global & geopolitical",
    "liquidity": "Liquidity & positioning",
}
MACRO_SECTION_BLURBS = {
    "policy_rates": "FOMC path, the curve, real yields",
    "growth_inflation": "CPI/PCE, payrolls, ISM, GDP",
    "internals": "VIX, breadth, credit spreads",
    "cross_asset": "Dollar, bonds, commodities",
    "global_geo": "Overnight tape, central banks, policy risk",
    "liquidity": "Balance sheet, issuance, seasonality",
}


class MacroAnalysis(Base):
    """One row per macro SECTION — the written analysis behind the /macro board.

    Same shape and lifecycle as CompanyAnalysis (agent-pushed or
    moderator-edited, carrying provenance), minus the symbol: macro sections are
    a fixed global taxonomy, not per-ticker. The computed parts of a section (the
    cross-asset strip, breadth) are NOT stored here — they're derived live on
    render, so they can never go stale in the database.
    """

    __tablename__ = "macro_analysis"

    id = Column(Integer, primary_key=True)
    section = Column(String(32), unique=True, nullable=False, index=True)  # MACRO_SECTIONS
    body = Column(Text, nullable=True)             # long-form prose
    content = Column(JSON, nullable=True)          # structured payload (tables/lists)
    sources = Column(JSON, nullable=True)          # [{title, url, kind}]
    source_kind = Column(String(16), nullable=False, default="manual")  # manual | agent | feed
    confidence = Column(String(16), nullable=True)  # high | medium | low
    as_of = Column(DateTime, default=_utcnow)
    updated_by = Column(String(120), nullable=True)


class MacroIndicator(Base):
    """Definition of ONE tracked macro series — the "Track" step of the study loop.

    Definitions live in the DB (not code) so the indicator set is a trading
    judgement the user can revise without a deploy. See MACRO_STUDY_DESIGN.md.
    """

    __tablename__ = "macro_indicators"

    id = Column(Integer, primary_key=True)
    key = Column(String(40), unique=True, nullable=False, index=True)   # e.g. "t10y2y"
    section = Column(String(32), nullable=False, index=True)            # MACRO_SECTIONS
    label = Column(String(120), nullable=False)
    source = Column(String(12), nullable=False)        # fred | yahoo | computed
    source_ref = Column(String(60), nullable=True)     # FRED series id / Yahoo symbol
    unit = Column(String(20), nullable=True)           # %, index, $bn …
    # How to READ the stored level. Readings always hold the raw source value;
    # transforms are applied at read time so the store stays source-of-truth.
    transform = Column(String(10), nullable=False, default="level")     # level | yoy | mom
    higher_is = Column(String(12), nullable=True)      # risk_on | risk_off | neutral
    note = Column(Text, nullable=True)                 # provenance / caveat shown in the UI
    sort = Column(Integer, nullable=False, default=0)
    active = Column(Boolean, nullable=False, default=True)


class MacroReading(Base):
    """One observation of one indicator — the series behind the sparkline.

    Same shape as MATPHistory (the proven time-series pattern here). `vintage`
    carries the as-published date for revisable macro data so a future
    point-in-time backfill can avoid lookahead; null = final/unrevised.
    """

    __tablename__ = "macro_readings"

    id = Column(Integer, primary_key=True)
    indicator_key = Column(String(40), nullable=False, index=True)
    as_of = Column(DateTime, nullable=False, index=True)
    value = Column(Float, nullable=False)
    vintage = Column(DateTime, nullable=True)

    __table_args__ = (
        UniqueConstraint("indicator_key", "as_of", name="uq_macro_reading_key_asof"),
    )


class UserWatchlist(Base):
    """A ticker one user has starred — the per-user "My Watchlist".

    Deliberately just (user, symbol): the ticker's DATA (MATP/MBP, trend, signal)
    still lives in MATPLevel, shared by everyone. This table only records WHOSE
    list a symbol is on, so starring never duplicates market data and a symbol
    with no MATPLevel row yet can still be tracked (the board renders it with a
    live price and blank target columns until an MATP run fills it in).

    Scoping is by user_id on every read and write — one member can never see or
    modify another's list. Cascade-deletes with the user.
    """

    __tablename__ = "user_watchlist"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"),
                     nullable=False, index=True)
    symbol = Column(String(20), nullable=False, index=True)
    note = Column(Text, nullable=True)          # optional "why I'm watching this"
    created_at = Column(DateTime, default=_utcnow)

    user = relationship("User")

    __table_args__ = (
        UniqueConstraint("user_id", "symbol", name="uq_user_watchlist_user_symbol"),
    )


class ChartDrawing(Base):
    """One user's chart drawings for one symbol — the shapes they drew on the
    price chart (horizontal lines, trend lines, rectangle zones).

    **One row per (user, symbol), holding the whole shape list as JSON.** The
    client already edits the list as a unit (draw / erase / clear-all rewrite the
    array), and nothing ever queries an individual shape, so a row-per-shape
    would only add write churn and a join for no query benefit. ``JSON`` is a
    portable column type — same code on SQLite and Postgres.

    Shape schema (validated server-side in routes/drawings.py):
      {"type": "hline", "p": <price>}
      {"type": "tline"|"rect", "a": {t, o, p}, "b": {t, o, p}}
      {"type": "trade", "a": {t, o, p=entry}, "b": {t, o, p}, "sl": <price>, "pt": <price>}
    where a point is date-anchored: ``t`` = the nearest candle's date, ``o`` = a
    fractional offset in bar-units from it, ``p`` = the price. Anchoring to a
    DATE (not a logical bar index) keeps a shape glued to the same spot when
    tomorrow's bar arrives or the history window slides.

    Scoping is by user_id on every read and write — one member can never see or
    modify another's drawings. Cascade-deletes with the user.
    """

    __tablename__ = "chart_drawings"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"),
                     nullable=False, index=True)
    symbol = Column(String(20), nullable=False, index=True)
    shapes = Column(JSON, nullable=False, default=list)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow)

    user = relationship("User")

    __table_args__ = (
        UniqueConstraint("user_id", "symbol", name="uq_chart_drawing_user_symbol"),
    )


class OptionSpread(Base):
    """A vertical option spread the member is tracking, so the platform can grade
    it against the management rule every time they look.

    Scope: **tracking, not execution.** Nothing here places, modifies or cancels an
    order — the fill happens in TWS by the user's own hand. These rows record what
    they say they opened so the delta line can be watched against it.

    Legs are stored as plain numbers rather than IBKR conIds because the contract
    is re-resolved from (symbol, expiry, strike) on each refresh anyway, and that
    keeps a tracked trade readable if TWS is off or the account changes.
    """

    __tablename__ = "option_spreads"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"),
                     nullable=False, index=True)
    symbol = Column(String(20), nullable=False, index=True)
    strategy = Column(String(24), nullable=False, default="bull_put")
    expiry = Column(String(10), nullable=False)          # YYYY-MM-DD
    short_strike = Column(Float, nullable=False)
    long_strike = Column(Float, nullable=False)
    credit = Column(Float, nullable=True)                # per share, at entry
    contracts = Column(Integer, nullable=False, default=1)
    entry_delta = Column(Float, nullable=True)           # short-put delta at entry
    opened_at = Column(DateTime, default=_utcnow)
    status = Column(String(12), nullable=False, default="open")   # open | closed
    closed_at = Column(DateTime, nullable=True)
    note = Column(Text, nullable=True)

    user = relationship("User")


class CompanyGuidance(Base):
    """One forward-guidance figure a company issued, as stated in an SEC filing.

    Rows are pushed by the Nous agent's ``guidance`` skill, which reads the 8-K
    item-2.02 press release and separates guidance from results -- a judgement a
    regex cannot make, since "revenue grew 5.9%" and "we expect revenue growth of
    5.9%" are lexically near-identical (measured: a regex extractor scored 7/15
    with false positives on NVDA's and WMT's results tables).

    ``sentence`` and ``source_url`` are NOT decoration: every number must be
    traceable to the exact wording in the filing it came from, so a figure can be
    audited -- and so an LLM can never quietly invent one. A row whose value does
    not appear in its own sentence is a bug, not a rounding difference.

    Grain: one row per (symbol, period, metric, basis). "period" is the fiscal
    period being GUIDED (e.g. FY2027-Q3), not the period being reported.
    """

    __tablename__ = "company_guidance"

    id = Column(Integer, primary_key=True)
    symbol = Column(String(20), nullable=False, index=True)
    period = Column(String(20), nullable=False)        # guided period, e.g. "FY2027-Q3"
    filed = Column(String(10), nullable=True)          # filing date, ISO
    metric = Column(String(40), nullable=False)        # revenue | gross_margin | eps | ...
    basis = Column(String(20), nullable=True)          # GAAP | non-GAAP | None
    unit = Column(String(20), nullable=True)           # USD_B | percent | USD_per_share
    low = Column(Float, nullable=True)
    mid = Column(Float, nullable=True)
    high = Column(Float, nullable=True)
    sentence = Column(Text, nullable=True)             # verbatim source wording
    source_url = Column(Text, nullable=True)           # exact exhibit URL
    accession = Column(String(30), nullable=True)      # SEC accession number
    created_at = Column(DateTime, default=_utcnow)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow)

    __table_args__ = (
        UniqueConstraint("symbol", "period", "metric", "basis",
                         name="uq_company_guidance_row"),
    )
