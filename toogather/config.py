"""
Configuration for TooGather.

All settings come from environment variables (usually a `.env` file read by
Docker Compose). Keeping every setting in one place means a new maintainer
only has to read this file to know what can be configured.

Safety rule: the app refuses to start with an insecure SECRET_KEY, because
that key signs sessions. A weak key would let anyone forge one.

SECRET_KEY does not have to be written by hand. If it is not set, the app
generates a strong one on first start and keeps it in TOOGATHER_DATA_DIR so
the same key is reused on the next start. Setting SECRET_KEY explicitly still
wins, which is what you want when several machines serve the same install.
"""

from __future__ import annotations

import logging
import os
import secrets
import stat
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

log = logging.getLogger("toogather.config")

# Values that must never be used in a real installation.
_INSECURE_SECRET_KEYS = {"", "change-me", "changeme", "secret"}

# Where generated state lives. A Docker volume is mounted here.
DEFAULT_DATA_DIR = "/data"
SECRET_KEY_FILE = "secret_key"


def resolve_secret_key() -> str:
    """
    The signing key: from the environment, from disk, or newly generated.

    Order of preference:
      1. SECRET_KEY in the environment - an explicit choice always wins.
      2. A key generated on an earlier start, stored in the data directory.
      3. A fresh key, written to the data directory for next time.

    If the data directory cannot be written to, the key is still returned so
    the app starts, but it will differ on the next start and everyone will be
    signed out. That is noisy rather than silent: it is logged as a warning.
    """
    from_env = os.environ.get("SECRET_KEY", "").strip()
    if from_env:
        return from_env

    data_dir = Path(os.environ.get("TOOGATHER_DATA_DIR", DEFAULT_DATA_DIR))
    key_path = data_dir / SECRET_KEY_FILE

    try:
        existing = key_path.read_text(encoding="utf-8").strip()
        if existing:
            return existing
    except (OSError, UnicodeDecodeError):
        pass  # not there yet, or unreadable: fall through and make one

    generated = secrets.token_urlsafe(48)
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        key_path.write_text(generated, encoding="utf-8")
        # Readable only by the account running the app.
        key_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
        log.info("Generated a new SECRET_KEY and saved it to %s", key_path)
    except OSError as exc:
        log.warning(
            "Could not save a generated SECRET_KEY to %s (%s). The app will run, but "
            "everyone will be signed out when it restarts. Mount a writable volume "
            "there, or set SECRET_KEY in your .env.",
            key_path, exc,
        )
    return generated


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

    # --- Connectors ---------------------------------------------------------
    # A connector pulls material into a project on a schedule. They run in the
    # worker, never in a web request, and can be switched off for the whole
    # server without touching any project.
    data_dir: str                    # writable state: the generated secret key, git mirrors
    connectors_enabled: bool
    connector_timeout_seconds: int   # how long one connector run may take

    @property
    def llm_configured(self) -> bool:
        """AI extraction can only run when an endpoint and a model are set."""
        return bool(self.llm_base_url and self.llm_model)

    @property
    def smtp_configured(self) -> bool:
        return bool(self.smtp_host and self.smtp_from)

    @property
    def connector_workdir(self) -> Path:
        """
        Where connectors keep their private state, e.g. the Git connector's
        mirrors. Inside the data directory, so one Docker volume covers
        everything TooGather writes to disk.
        """
        return Path(self.data_dir) / "connectors"

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
        secret_key=resolve_secret_key(),
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
        data_dir=os.environ.get("TOOGATHER_DATA_DIR", DEFAULT_DATA_DIR),
        connectors_enabled=_env_bool("CONNECTORS_ENABLED", True),
        connector_timeout_seconds=_env_int("CONNECTOR_TIMEOUT_SECONDS", 300),
    )


def require_secure_secret(settings: Settings) -> None:
    """
    Stop startup with a clear message if SECRET_KEY is weak.

    A key is normally generated automatically, so reaching this means someone
    set a bad one on purpose.
    """
    key = settings.secret_key
    if key.strip().lower() in _INSECURE_SECRET_KEYS or len(key) < 32:
        raise SystemExit(
            "SECRET_KEY is set but too weak (minimum 32 characters).\n"
            "Remove it from your .env to have one generated automatically, or\n"
            'generate one yourself with:  python -c "import secrets; '
            'print(secrets.token_urlsafe(48))"'
        )
