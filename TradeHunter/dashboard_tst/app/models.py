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
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import relationship

from .db import Base

# status values
PENDING = "pending"
APPROVED = "approved"
DISABLED = "disabled"


def _utcnow() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    email = Column(String(255), unique=True, nullable=False, index=True)
    google_sub = Column(String(64), unique=True, nullable=True, index=True)  # OIDC subject
    display_name = Column(String(120), nullable=False, default="")
    picture = Column(String(512), nullable=True)
    role = Column(String(20), nullable=False, default="member")   # member | admin
    status = Column(String(20), nullable=False, default=PENDING)  # pending | approved | disabled
    created_at = Column(DateTime, default=_utcnow)
    approved_at = Column(DateTime, nullable=True)

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

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
