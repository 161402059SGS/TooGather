"""
The background worker.

It does two jobs in a simple loop:
  1. Process uploaded sources: send them to the AI extractor (if allowed) and
     save the proposals for review.
  2. Once per DIGEST_INTERVAL_DAYS, email each project's owners a digest.

Why a loop instead of a job-queue library: the queue is just the `sources`
table (see repo.claim_next_pending_source). One less service to install,
monitor, and explain.

Run with:  python -m toogather.worker
"""

from __future__ import annotations

import logging
import signal
import time
from datetime import UTC, datetime, timedelta

from toogather import db, repo
from toogather.config import Settings, load_settings
from toogather.digest import render_digest_text, send_email
from toogather.extraction import ExtractionError, extract_events
from toogather.services import project_brief

log = logging.getLogger("toogather.worker")

IDLE_SLEEP_SECONDS = 5          # how long to wait when there is no work
DIGEST_CHECK_EVERY_SECONDS = 600
_running = True


def _stop(signum, _frame) -> None:
    """Finish the current job, then exit cleanly (e.g. on `docker compose stop`)."""
    global _running
    log.info("Received signal %s, stopping after the current job.", signum)
    _running = False


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
    if not settings.llm_configured:
        repo.finish_source(source_id, "skipped",
                           "No AI endpoint is configured. Set LLM_BASE_URL and LLM_MODEL.")
        return True

    try:
        proposals = extract_events(settings, source["filename"], source["content"])
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

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    log.info("Worker started. AI extraction configured: %s. Email configured: %s.",
             settings.llm_configured, settings.smtp_configured)

    last_digest_check = 0.0
    while _running:
        try:
            did_work = process_one_source(settings)
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
