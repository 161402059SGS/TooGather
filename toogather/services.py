"""
Small pieces of logic that combine the database with the pure rules.

Web pages, the API, and the worker all call these functions, so a project
brief looks identical everywhere.
"""

from __future__ import annotations

from toogather import repo
from toogather.config import Settings
from toogather.drift import Finding, build_brief, detect_drift


def project_findings(settings: Settings, project_id: str,
                     events: list[dict] | None = None) -> list[Finding]:
    events = events if events is not None else repo.events_for_drift(project_id)
    return detect_drift(
        events,
        settings.today(),
        stale_question_days=settings.stale_question_days,
        stale_proposal_days=settings.stale_proposal_days,
    )


def project_brief(settings: Settings, project_id: str) -> dict:
    events = repo.events_for_drift(project_id)
    findings = project_findings(settings, project_id, events)
    return build_brief(events, findings)
