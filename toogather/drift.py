"""
Drift detection: find things a project team has probably forgotten.

These rules are deliberately plain Python, not AI. Plain rules are:
  * predictable: the same data always gives the same findings,
  * explainable: every finding says exactly why it was raised,
  * testable: see tests/test_drift.py,
  * free: no LLM calls, so they can run as often as needed.

Every rule is a pure function of (events, today, settings). It reads a list of
event dicts (as returned by repo.events_for_drift) and returns findings.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime

from toogather.models import EventStatus, EventType


@dataclass(frozen=True)
class Finding:
    rule: str          # machine name, e.g. "overdue_commitment"
    severity: str      # "high" needs action now; "medium" needs attention soon
    message: str       # one sentence a PM can act on
    event_id: str
    summary: str


def _as_date(value: date | datetime | None) -> date | None:
    """created_at comes back as a datetime; due_date as a date. Compare them as dates."""
    if isinstance(value, datetime):
        return value.date()
    return value


def overdue_commitments(events: list[dict], today: date, **_: object) -> list[Finding]:
    """A confirmed commitment whose due date has passed without being marked done."""
    findings = []
    for e in events:
        due = _as_date(e.get("due_date"))
        if (e["type"] == EventType.COMMITMENT and e["status"] == EventStatus.CONFIRMED
                and due is not None and due < today):
            days = (today - due).days
            who = e.get("owner") or "no owner recorded"
            findings.append(Finding(
                rule="overdue_commitment",
                severity="high",
                message=f"Overdue by {days} day{'s' if days != 1 else ''} ({who}).",
                event_id=str(e["id"]),
                summary=e["summary"],
            ))
    return findings


def unlinked_changes(events: list[dict], today: date, **_: object) -> list[Finding]:
    """
    A confirmed change that is not linked to any decision.
    This is the "someone changed it but never told the PM" signal.
    """
    return [
        Finding(
            rule="unlinked_change",
            severity="high",
            message="Change is not linked to a decision. Confirm it was agreed.",
            event_id=str(e["id"]),
            summary=e["summary"],
        )
        for e in events
        if e["type"] == EventType.CHANGE
        and e["status"] in (EventStatus.CONFIRMED, EventStatus.DONE)
        and not e.get("related_id")
    ]


def stale_questions(events: list[dict], today: date, *, stale_question_days: int = 14,
                    **_: object) -> list[Finding]:
    """An open question nobody has answered for a long time."""
    findings = []
    for e in events:
        created = _as_date(e.get("created_at"))
        if (e["type"] == EventType.QUESTION and e["status"] == EventStatus.CONFIRMED
                and created is not None and (today - created).days > stale_question_days):
            findings.append(Finding(
                rule="stale_question",
                severity="medium",
                message=f"Unanswered for {(today - created).days} days.",
                event_id=str(e["id"]),
                summary=e["summary"],
            ))
    return findings


def risks_without_owner(events: list[dict], today: date, **_: object) -> list[Finding]:
    """An open risk that nobody is responsible for."""
    return [
        Finding(
            rule="risk_without_owner",
            severity="medium",
            message="Open risk has no owner.",
            event_id=str(e["id"]),
            summary=e["summary"],
        )
        for e in events
        if e["type"] == EventType.RISK
        and e["status"] == EventStatus.CONFIRMED
        and not (e.get("owner") or "").strip()
    ]


def stale_proposals(events: list[dict], today: date, *, stale_proposal_days: int = 3,
                    **_: object) -> list[Finding]:
    """AI proposals nobody has reviewed. Unreviewed memory is untrusted memory."""
    findings = []
    for e in events:
        created = _as_date(e.get("created_at"))
        if (e["status"] == EventStatus.PROPOSED and created is not None
                and (today - created).days > stale_proposal_days):
            findings.append(Finding(
                rule="stale_proposal",
                severity="medium",
                message=f"Waiting for review for {(today - created).days} days.",
                event_id=str(e["id"]),
                summary=e["summary"],
            ))
    return findings


# The full rule set, in the order findings are shown.
RULES: list[Callable[..., list[Finding]]] = [
    overdue_commitments,
    unlinked_changes,
    risks_without_owner,
    stale_questions,
    stale_proposals,
]


def detect_drift(events: list[dict], today: date, *, stale_question_days: int = 14,
                 stale_proposal_days: int = 3) -> list[Finding]:
    """Run every rule and return all findings, high severity first."""
    findings: list[Finding] = []
    for rule in RULES:
        findings.extend(rule(events, today, stale_question_days=stale_question_days,
                             stale_proposal_days=stale_proposal_days))
    return sorted(findings, key=lambda f: 0 if f.severity == "high" else 1)


def build_brief(events: list[dict], findings: list[Finding], limit: int = 15) -> dict:
    """
    A compact summary of where a project stands. Used by the web overview,
    the weekly digest, and the MCP get_project_brief tool, so all three always
    agree with each other.
    """

    def pick(type_: str, statuses: tuple[str, ...]) -> list[dict]:
        rows = [e for e in events if e["type"] == type_ and e["status"] in statuses]
        rows.sort(key=lambda e: e["created_at"], reverse=True)
        return [
            {
                "id": str(e["id"]),
                "summary": e["summary"],
                "owner": e.get("owner"),
                "due_date": e["due_date"].isoformat() if e.get("due_date") else None,
                "source": e.get("source_ref") or None,
                "recorded": _as_date(e["created_at"]).isoformat(),
            }
            for e in rows[:limit]
        ]

    open_ = (EventStatus.CONFIRMED.value,)
    return {
        "decisions": pick(EventType.DECISION, (EventStatus.CONFIRMED.value, EventStatus.DONE.value)),
        "open_commitments": pick(EventType.COMMITMENT, open_),
        "open_risks": pick(EventType.RISK, open_),
        "open_questions": pick(EventType.QUESTION, open_),
        "recent_changes": pick(EventType.CHANGE, (EventStatus.CONFIRMED.value, EventStatus.DONE.value)),
        "awaiting_review": sum(1 for e in events if e["status"] == EventStatus.PROPOSED),
        "attention": [f.__dict__ for f in findings],
    }
