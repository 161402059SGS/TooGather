"""
Configuration for TooGather.

All settings come from environment variables (usually a `.env` file read by
Docker Compose). Keeping every setting in one place means a new maintainer
only has to read this file to know what can be configured.

Safety rule: the app refuses to start with an insecure SECRET_KEY, because
that key signs login sessions. A weak key would let anyone forge a login.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date, datetime
from zoneinfo import ZoneInfo

# Values that must never be used in a real installation.
_INSECURE_SECRET_KEYS = {"", "change-me", "changeme", "secret"}


def _env_bool(name: str, default: bool) -> bool:
    """Read a true/false environment variable ("1", "true", "yes" count as true)."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    """Read a whole-number environment variable, falling back to a default."""
    raw = os.environ.get(name)
    return int(raw) if raw and raw.strip() else default


@dataclass(frozen=True)
class Settings:
    # --- Core -------------------------------------------------------------
    database_url: str
    secret_key: str
    base_url: str               # how people reach the app, used in digest links
    timezone: str               # the team's time zone, e.g. Asia/Jakarta; decides what "today" is
    cookie_secure: bool         # set true when served over HTTPS

    # --- AI extraction (optional) -----------------------------------------
    # Any OpenAI-compatible chat endpoint works: OpenAI, Anthropic's
    # OpenAI-compatible endpoint, OpenRouter, or a local Ollama.
    llm_base_url: str
    llm_api_key: str
    llm_model: str
    llm_timeout_seconds: int
    llm_max_input_chars: int    # long documents are split into chunks of this size

    # --- Weekly digest by email (optional) --------------------------------
    smtp_host: str
    smtp_port: int
    smtp_username: str
    smtp_password: str
    smtp_from: str
    smtp_starttls: bool
    digest_interval_days: int

    # --- Drift rules --------------------------------------------------------
    stale_question_days: int    # an open question older than this is flagged
    stale_proposal_days: int    # an unreviewed AI proposal older than this is flagged

    @property
    def llm_configured(self) -> bool:
        """AI extraction can only run when an endpoint and a model are set."""
        return bool(self.llm_base_url and self.llm_model)

    @property
    def smtp_configured(self) -> bool:
        return bool(self.smtp_host and self.smtp_from)

    def today(self) -> date:
        """
        Today's date in the team's time zone.

        Servers usually run in UTC. Without this, a commitment due "today" in
        Jakarta (UTC+7) could be flagged overdue or not depending on the hour.
        """
        return datetime.now(ZoneInfo(self.timezone)).date()


def load_settings() -> Settings:
    """Build Settings from the environment. Called once at startup."""
    return Settings(
        database_url=os.environ.get(
            "DATABASE_URL", "postgresql://toogather:toogather@localhost:5432/toogather"
        ),
        secret_key=os.environ.get("SECRET_KEY", ""),
        base_url=os.environ.get("BASE_URL", "http://localhost:8080").rstrip("/"),
        timezone=os.environ.get("TIMEZONE", "UTC"),
        cookie_secure=_env_bool("COOKIE_SECURE", False),
        llm_base_url=os.environ.get("LLM_BASE_URL", "").rstrip("/"),
        llm_api_key=os.environ.get("LLM_API_KEY", ""),
        llm_model=os.environ.get("LLM_MODEL", ""),
        llm_timeout_seconds=_env_int("LLM_TIMEOUT_SECONDS", 120),
        llm_max_input_chars=_env_int("LLM_MAX_INPUT_CHARS", 12000),
        smtp_host=os.environ.get("SMTP_HOST", ""),
        smtp_port=_env_int("SMTP_PORT", 587),
        smtp_username=os.environ.get("SMTP_USERNAME", ""),
        smtp_password=os.environ.get("SMTP_PASSWORD", ""),
        smtp_from=os.environ.get("SMTP_FROM", ""),
        smtp_starttls=_env_bool("SMTP_STARTTLS", True),
        digest_interval_days=_env_int("DIGEST_INTERVAL_DAYS", 7),
        stale_question_days=_env_int("STALE_QUESTION_DAYS", 14),
        stale_proposal_days=_env_int("STALE_PROPOSAL_DAYS", 3),
    )


def require_secure_secret(settings: Settings) -> None:
    """Stop startup with a clear message if SECRET_KEY is missing or weak."""
    key = settings.secret_key
    if key.strip().lower() in _INSECURE_SECRET_KEYS or len(key) < 32:
        raise SystemExit(
            "SECRET_KEY is missing or too short (minimum 32 characters).\n"
            "Generate one with:  python -c \"import secrets; print(secrets.token_urlsafe(48))\"\n"
            "and put it in your .env file."
        )
