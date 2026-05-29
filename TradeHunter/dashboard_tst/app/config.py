"""Environment-driven settings. Copy ``.env.example`` -> ``.env`` (gitignored)
and fill in. All keys are prefixed ``TST_`` so they don't collide with the
trading bot's own env.

Deliberately dependency-light (stdlib ``os.environ``) so the scaffold has
no settings-library coupling.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _csv(value: str | None) -> tuple[str, ...]:
    if not value:
        return ()
    return tuple(p.strip().lower() for p in value.split(",") if p.strip())


@dataclass
class Settings:
    # Core
    secret_key: str = field(
        default_factory=lambda: os.environ.get("TST_SECRET_KEY", "dev-insecure-change-me")
    )
    database_url: str = field(
        default_factory=lambda: os.environ.get("TST_DATABASE_URL", "sqlite:///./tst.db")
    )
    debug: bool = field(default_factory=lambda: _bool(os.environ.get("TST_DEBUG"), False))

    # --- Google OAuth (Sign in with Google) ---
    google_client_id: str | None = field(
        default_factory=lambda: os.environ.get("TST_GOOGLE_CLIENT_ID")
    )
    google_client_secret: str | None = field(
        default_factory=lambda: os.environ.get("TST_GOOGLE_CLIENT_SECRET")
    )
    # Optional explicit redirect URI. Behind a TLS reverse proxy the
    # auto-derived URL may be http://, so set this in prod to the exact
    # https URI registered in the Google Cloud OAuth client, e.g.
    #   https://study.example.com/auth/callback
    oauth_redirect_uri: str | None = field(
        default_factory=lambda: os.environ.get("TST_OAUTH_REDIRECT_URI")
    )

    # Authorization model: Google authenticates; the admin approves.
    # The single email that is auto-promoted to admin + approved on first
    # sign-in (bootstrap). Everyone else starts 'pending'.
    admin_email: str | None = field(default_factory=lambda: os.environ.get("TST_ADMIN_EMAIL"))
    # Optional hard gate: if set, only these email domains may even create
    # a pending account (cuts approval-queue spam). Comma-separated.
    allowed_email_domains: tuple[str, ...] = field(
        default_factory=lambda: _csv(os.environ.get("TST_ALLOWED_EMAIL_DOMAINS"))
    )

    # Session cookie
    session_cookie: str = "tst_session"
    # Set TST_HTTPS_ONLY=1 in production (behind TLS).
    session_https_only: bool = field(
        default_factory=lambda: _bool(os.environ.get("TST_HTTPS_ONLY"), False)
    )

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    @property
    def secret_is_default(self) -> bool:
        return self.secret_key == "dev-insecure-change-me"

    @property
    def google_configured(self) -> bool:
        return bool(self.google_client_id and self.google_client_secret)

    def domain_allowed(self, email: str) -> bool:
        if not self.allowed_email_domains:
            return True
        try:
            return email.rsplit("@", 1)[1].lower() in self.allowed_email_domains
        except IndexError:
            return False


settings = Settings()
