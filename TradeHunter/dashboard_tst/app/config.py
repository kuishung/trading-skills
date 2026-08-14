"""Environment-driven settings. Copy ``.env.example`` -> ``.env`` (gitignored)
and fill in. All keys are prefixed ``TST_`` so they don't collide with the
trading bot's own env.

Deliberately dependency-light (stdlib ``os.environ``) so the scaffold has
no settings-library coupling.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

# Load app/.env into the process environment (if present) so the settings
# below resolve from it. Gitignored, per-PC. No-op if python-dotenv or the
# file is missing.
try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parent / ".env")
except Exception:
    pass

AUTH_PASSWORD = "password"
AUTH_GOOGLE = "google"


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

    # --- Auth mode ---
    # "password" (Path B): admin seeds accounts with passwords; access is
    #   gated by the Hamachi VPN + login. No domain/TLS/Google needed.
    # "google"  (Path A): Google OAuth + admin approval. Requires an https
    #   domain (Google rejects bare/private IPs as redirect URIs).
    auth_mode: str = field(
        default_factory=lambda: (os.environ.get("TST_AUTH_MODE", AUTH_PASSWORD) or AUTH_PASSWORD).strip().lower()
    )

    # Admin bootstrap.
    # password mode: TST_ADMIN_EMAIL + TST_ADMIN_PASSWORD seed an approved
    #   admin on first startup.
    # google mode: TST_ADMIN_EMAIL is auto-promoted to admin on first sign-in.
    admin_email: str | None = field(default_factory=lambda: os.environ.get("TST_ADMIN_EMAIL"))
    admin_password: str | None = field(default_factory=lambda: os.environ.get("TST_ADMIN_PASSWORD"))

    # Shared key for the machine-to-machine /api/* endpoints (the Nous Hermes
    # agent pushes MATP levels here). Unset -> those endpoints return 503.
    ingest_api_key: str | None = field(default_factory=lambda: os.environ.get("TST_INGEST_API_KEY"))

    # Where to archive each MATP run as a JSON file (the raw extraction, the
    # "cream"). On Hermes set this to the Resilio-synced MarketData path, e.g.
    # C:\HermesSync\MarketData\MATP. Empty -> defaults to <TradeHunter>/data/MATP.
    matp_dir: str | None = field(default_factory=lambda: os.environ.get("TST_MATP_DIR"))

    # Parquet bars store root, for the Data Ingest page's HEALTH check (freshness
    # only — file metadata, never parquet content; parquet stays backtest-only).
    # On Hermes set to the Resilio path, e.g. C:\HermesSync\MarketData\price_history.
    # Empty -> the Parquet-ingest health section shows "not configured".
    price_history_dir: str | None = field(
        default_factory=lambda: os.environ.get("TST_PRICE_HISTORY_DIR")
    )

    # EDGAR quarterly-report corpus root — per-ticker folders of 10-Q/10-K files
    # (html + cleaned .md), for the Company page's Earnings-report tab. On
    # Hermes/AI-Hermes this is the Resilio-synced
    # C:\HermesSync\MarketResearch\QuarterlyReport. When unset/unreadable the tab
    # falls back to the pushed EdgarIngestHealth inventory (list only, no bodies).
    edgar_dir: str | None = field(default_factory=lambda: os.environ.get("TST_EDGAR_DIR"))

    # Discord: outbound webhook URL for collaboration notifications (e.g. a MATP
    # refresh completing posts a summary to a channel). Unset -> notifications
    # are silently skipped. Create it in Discord: Server Settings -> Integrations
    # -> Webhooks -> New Webhook -> Copy URL.
    discord_webhook_url: str | None = field(
        default_factory=lambda: os.environ.get("TST_DISCORD_WEBHOOK_URL")
    )

    # Discord BOT (read path): webhooks are write-only, so to SHOW a study's
    # discussion we use a bot to create one thread per study and read its
    # messages back. Create a bot at discord.com/developers (enable the MESSAGE
    # CONTENT intent), invite it to the server with: Create Public Threads, Send
    # Messages in Threads, Read Message History. Then set the token + the parent
    # channel id where study threads are created. Guild id (optional) is only for
    # building "open in Discord" deep-links. Unset -> the discussion panel is
    # hidden and threads aren't created (webhook doorbell still works).
    discord_bot_token: str | None = field(
        default_factory=lambda: os.environ.get("TST_DISCORD_BOT_TOKEN")
    )
    discord_channel_id: str | None = field(
        default_factory=lambda: os.environ.get("TST_DISCORD_CHANNEL_ID")
    )
    discord_guild_id: str | None = field(
        default_factory=lambda: os.environ.get("TST_DISCORD_GUILD_ID")
    )

    # Public base URL used to build click-through links in outbound notifications
    # (the agent calls the API server-side, so request.base_url is internal).
    public_url: str = field(
        default_factory=lambda: (os.environ.get("TST_PUBLIC_URL") or "https://tradehunter.net").rstrip("/")
    )

    # New-user policy (google mode). Default OFF: a first-time sign-in lands
    # 'pending' (role = member) and must be APPROVED by an admin before they
    # get access; the admin can also change their role afterward. Set
    # TST_AUTO_APPROVE=1 to skip approval (new users become active members
    # automatically).
    auto_approve: bool = field(default_factory=lambda: _bool(os.environ.get("TST_AUTO_APPROVE"), False))

    # --- Google OAuth (only used when auth_mode == "google") ---
    google_client_id: str | None = field(
        default_factory=lambda: os.environ.get("TST_GOOGLE_CLIENT_ID")
    )
    google_client_secret: str | None = field(
        default_factory=lambda: os.environ.get("TST_GOOGLE_CLIENT_SECRET")
    )
    oauth_redirect_uri: str | None = field(
        default_factory=lambda: os.environ.get("TST_OAUTH_REDIRECT_URI")
    )
    allowed_email_domains: tuple[str, ...] = field(
        default_factory=lambda: _csv(os.environ.get("TST_ALLOWED_EMAIL_DOMAINS"))
    )

    # --- Research: planning chat talks to DeepSeek DIRECTLY (the Nous agent is
    # outbound-only, so it can't serve a real-time chat). OpenAI-compatible API,
    # so the same fields work for DeepSeek cloud or a local deployment — only the
    # base_url/key differ. Key lives in app/.env (gitignored) / the Vault, never
    # the repo. Unset key -> the chat endpoint returns a friendly "not configured".
    deepseek_api_key: str | None = field(
        default_factory=lambda: os.environ.get("TST_DEEPSEEK_API_KEY")
    )
    deepseek_base_url: str = field(
        default_factory=lambda: os.environ.get("TST_DEEPSEEK_BASE_URL", "https://api.deepseek.com")
    )
    deepseek_model: str = field(
        default_factory=lambda: os.environ.get("TST_DEEPSEEK_MODEL", "deepseek-chat")
    )

    # --- Research chat: agent-grounded mode (optional) ---
    # When TST_RESEARCH_RUNNER_URL is set, the Research planning chat is relayed
    # to the LAN-only research-runner shim on the Nous agent (Linux box) instead
    # of calling DeepSeek directly. The shim runs the REAL Hermes agent, so the
    # chat can read the EDGAR 10-Q corpus on demand. If the runner is unset or
    # unreachable, the chat falls back to DeepSeek-direct (deepseek_* above).
    # The token must match TST_RESEARCH_TOKEN in the runner's ~/.hermes/.env.
    research_runner_url: str | None = field(
        default_factory=lambda: os.environ.get("TST_RESEARCH_RUNNER_URL")
    )
    research_runner_token: str | None = field(
        default_factory=lambda: os.environ.get("TST_RESEARCH_TOKEN")
    )
    research_runner_timeout: float = field(
        default_factory=lambda: float(os.environ.get("TST_RESEARCH_RUNNER_TIMEOUT", "150"))
    )

    # Session cookie. Set TST_HTTPS_ONLY=1 only when served over TLS; over a
    # Hamachi VPN on plain http it must stay 0 (the VPN tunnel is encrypted).
    session_cookie: str = "tst_session"
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
    def is_google_auth(self) -> bool:
        return self.auth_mode == AUTH_GOOGLE

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
