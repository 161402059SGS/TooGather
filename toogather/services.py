"""
Small pieces of logic that combine the database with the pure rules.

Web pages, the API, and the worker all call these functions, so a project
brief looks identical everywhere.
"""

from __future__ import annotations

from dataclasses import replace

from toogather import repo
from toogather.config import Settings
from toogather.crypto import decrypt_secret
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


def settings_for_project(settings: Settings, project_id: str) -> Settings:
    """
    The server settings, with this project's own AI provider applied if it has one.

    Why per project: one client's contract forbids sending their documents to a
    cloud service while another team wants the best model available. Making
    that a server-wide choice means the stricter client decides for everyone,
    which in practice means AI extraction is switched off for everyone.

    A project only overrides when it has supplied both an endpoint and a model.
    A half-filled override would otherwise silently send a client's documents
    to the server default, which is exactly the outcome it exists to prevent.

    If the stored API key cannot be decrypted - SECRET_KEY changed since it was
    saved - the override is used with no key rather than quietly falling back
    to the server's key. That fails loudly at the provider, which is right: the
    alternative is sending a client's documents somewhere they did not choose.
    """
    override = repo.get_ai_settings(project_id)
    if not override:
        return settings
    base_url = (override["base_url"] or "").rstrip("/")
    model = override["model"] or ""
    if not base_url or not model:
        return settings

    return replace(
        settings,
        llm_base_url=base_url,
        llm_model=model,
        llm_api_key=decrypt_secret(settings.secret_key, override["api_key_encrypted"]) or "",
    )
