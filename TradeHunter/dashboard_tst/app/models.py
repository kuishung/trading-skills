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
    trend = Column(String(20), nullable=True)  # from resources.trend_state
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
    created_by = Column(Integer, ForeignKey("users.id"))
    created_at = Column(DateTime, default=_utcnow)

    author = relationship("User")


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
