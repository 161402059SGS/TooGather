"""
The background worker.

It does three jobs in a simple loop:
  1. Process uploaded sources: send them to the AI extractor (if the project
     allows it) and save the proposals for review.
  2. Run connectors that are due, so new material arrives without anyone
     having to remember to upload it.
  3. Once per DIGEST_INTERVAL_DAYS, email each project's owners a digest.

Why a loop instead of a job-queue library: both queues are just tables (see
repo.claim_next_pending_source and repo.claim_due_connector). One less service
to install, monitor, and explain.

Uploads come before connectors on purpose. Somebody is waiting for an upload
they just made; nobody is watching a repository poll.

Run with:  python -m toogather.worker
"""

from __future__ import annotations

import logging
import signal
import time
from datetime import UTC, datetime, timedelta

from toogather import connectors, db, repo
from toogather.config import Settings, load_settings
from toogather.connectors.base import ConnectorError, Context
from toogather.crypto import decrypt_secret
from toogather.digest import render_digest_text, send_email
from toogather.extraction import ExtractionError, extract_events
from toogather.services import project_brief, settings_for_project

log = logging.getLogger("toogather.worker")

IDLE_SLEEP_SECONDS = 5          # how long to wait when there is no work
DIGEST_CHECK_EVERY_SECONDS = 600
CONNECTOR_CHECK_EVERY_SECONDS = 30
_running = True


def _stop(signum, _frame) -> None:
    """Finish the current job, then exit cleanly (e.g. on `docker compose stop`)."""
    global _running
    log.info("Received signal %s, stopping after the current job.", signum)
    _running = False


# ---------------------------------------------------------------------
# 1. Uploaded and connector-supplied sources
# ---------------------------------------------------------------------


def process_one_source(settings: Settings) -> bool:
    """Process the next pending source. Returns True if there was one to process."""
    source = repo.claim_next_pending_source()
    if source is None:
        return False

    source_id = str(source["id"])
    log.info("Processing source %s (%s)", source_id, source["filename"])

    # Respect the project's decision about sending documents to AI.
    if not source["ai_extraction_enabled"]:
        repo.finish_source(source_id, "skipped",
                           "AI extraction is off for this project. Add events manually.")
        return True

    # A project may point at its own AI provider - a local Ollama for a client
    # whose contract forbids cloud services, say - so the provider is resolved
    # per project rather than read straight from the environment.
    project_settings = settings_for_project(settings, str(source["project_id"]))
    if not project_settings.llm_configured:
        repo.finish_source(source_id, "skipped",
                           "No AI endpoint is configured. Set one on this server, or "
                           "give this project its own under Settings.")
        return True

    try:
        proposals = extract_events(project_settings, source["filename"], source["content"])
        count = repo.insert_proposed_events(str(source["project_id"]), source, proposals)
        repo.finish_source(source_id, "done", f"{count} proposal(s) ready for review.")
        log.info("Source %s done: %d proposals", source_id, count)
    except ExtractionError as exc:
        repo.finish_source(source_id, "failed", str(exc))
        log.warning("Source %s failed: %s", source_id, exc)
    except Exception:  # one bad document must not stop the worker
        repo.finish_source(source_id, "failed", "Unexpected error. See worker logs.")
        log.exception("Unexpected error processing source %s", source_id)
    return True


# ---------------------------------------------------------------------
# 2. Connectors
# ---------------------------------------------------------------------


def _store_items(connector_row: dict, result) -> tuple[int, int]:
    """Save what a run produced. Returns (stored, already_here)."""
    stored = skipped = 0
    for item in result.items:
        row = repo.create_connector_source(
            project_id=str(connector_row["project_id"]),
            connector_id=str(connector_row["id"]),
            external_id=item.external_id,
            filename=item.title,
            content=item.content,
        )
        if row is None:
            skipped += 1      # the unique index refused a repeat; nothing to do
        else:
            stored += 1
    return stored, skipped


def run_one_connector(settings: Settings) -> bool:
    """
    Run the connector that is most overdue. Returns True if one ran.

    A failed run does not move the cursor, so the next run tries the same
    material again rather than stepping over it.
    """
    if not settings.connectors_enabled:
        return False
    row = repo.claim_due_connector()
    if row is None:
        return False

    connector_id = str(row["id"])
    connector = connectors.get(row["kind"])
    if connector is None:
        repo.finish_connector(
            connector_id, "failed",
            f"This server has no '{row['kind']}' connector installed.", None)
        return True

    log.info("Running connector %s (%s) for project %s",
             connector_id, row["kind"], row["project_id"])
    try:
        workdir = settings.connector_workdir
        workdir.mkdir(parents=True, exist_ok=True)
        result = connector.fetch(Context(
            connector_id=connector_id,
            project_id=str(row["project_id"]),
            config=dict(row["config"] or {}),
            secret=decrypt_secret(settings.secret_key, row["secret_encrypted"]) or "",
            cursor=row["cursor"] or "",
            workdir=workdir,
            timeout_seconds=settings.connector_timeout_seconds,
        ))
        stored, skipped = _store_items(row, result)
    except ConnectorError as exc:
        repo.finish_connector(connector_id, "failed", str(exc), None)
        log.warning("Connector %s failed: %s", connector_id, exc)
        return True
    except Exception:  # one broken connector must not stop the worker
        repo.finish_connector(connector_id, "failed",
                              "Unexpected error. See the worker logs.", None)
        log.exception("Unexpected error running connector %s", connector_id)
        return True

    note = result.note or f"Brought in {stored} item(s)."
    if skipped:
        note += f" {skipped} were already here."
    repo.finish_connector(connector_id, "ok", note, result.cursor or None)
    log.info("Connector %s: %s", connector_id, note)
    return True


# ---------------------------------------------------------------------
# 3. The weekly digest
# ---------------------------------------------------------------------


def send_digests_if_due(settings: Settings) -> None:
    """Send the digest to every project's owners, at most once per interval."""
    if not settings.smtp_configured:
        return

    last_sent = repo.get_state("last_digest_at")
    now = datetime.now(UTC)
    if last_sent and now - datetime.fromisoformat(last_sent) < timedelta(
            days=settings.digest_interval_days):
        return

    for project_id in repo.all_project_ids():
        project = repo.get_project(project_id)
        recipients = [r["email"] for r in repo.project_digest_recipients(project_id)]
        if not project or not recipients:
            continue
        brief = project_brief(settings, project_id)
        body = render_digest_text(project, brief, settings.base_url)
        try:
            send_email(settings, recipients, f"Weekly summary: {project['name']}", body)
            log.info("Digest sent for project %s to %d owner(s)", project_id, len(recipients))
        except Exception as exc:  # keep going with other projects
            log.warning("Could not send digest for project %s: %s", project_id, exc)

    # Record the attempt even if some emails failed, to avoid emailing people
    # repeatedly every few minutes while an SMTP problem is being fixed.
    repo.set_state("last_digest_at", now.isoformat())


# ---------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = load_settings()
    db.init_pool(settings.database_url, max_size=3)
    applied = db.run_migrations()
    if applied:
        log.info("Applied migrations: %s", ", ".join(applied))

    reset = repo.reset_stuck_sources()
    if reset:
        log.info("Re-queued %d source(s) left 'processing' by a previous run.", reset)
    released = repo.release_running_connectors()
    if released:
        log.info("Released %d connector(s) left running by a previous run.", released)

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    log.info(
        "Worker started. AI extraction configured: %s. Email configured: %s. "
        "Connectors: %s (%s).",
        settings.llm_configured, settings.smtp_configured,
        "on" if settings.connectors_enabled else "off",
        ", ".join(c.kind for c in connectors.available()) or "none installed",
    )

    last_digest_check = last_connector_check = 0.0
    while _running:
        did_work = False
        try:
            did_work = process_one_source(settings)

            # Connectors only get a turn when no upload is waiting: somebody is
            # watching an upload, nobody is watching a repository poll.
            if not did_work and (
                    time.monotonic() - last_connector_check > CONNECTOR_CHECK_EVERY_SECONDS):
                last_connector_check = time.monotonic()
                did_work = run_one_connector(settings)

            if time.monotonic() - last_digest_check > DIGEST_CHECK_EVERY_SECONDS:
                send_digests_if_due(settings)
                last_digest_check = time.monotonic()
        except Exception:  # e.g. database briefly unavailable
            log.exception("Worker loop error; retrying shortly.")
            did_work = False
        if not did_work:
            time.sleep(IDLE_SLEEP_SECONDS)

    db.close_pool()
    log.info("Worker stopped.")


if __name__ == "__main__":
    main()
