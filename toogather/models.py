"""
The vocabulary of TooGather.

These constants and models are shared by the database layer, the web app,
the AI extractor, and the API. If you add a new event type or status, change
it here AND in a new SQL migration (the database has CHECK constraints).
"""

from __future__ import annotations

from datetime import date
from enum import StrEnum

from pydantic import BaseModel, Field, field_validator


class EventType(StrEnum):
    """The five kinds of project knowledge TooGather tracks."""

    DECISION = "decision"      # "We use batch numbering for finished goods"
    COMMITMENT = "commitment"  # "Budi sends the mapping file by Friday"
    CHANGE = "change"          # "Procedure X was modified in production"
    RISK = "risk"              # "Client master data is not cleaned yet"
    QUESTION = "question"      # "Who approves credit limit overrides?"


class EventStatus(StrEnum):
    PROPOSED = "proposed"      # suggested (usually by AI), waiting for a person
    CONFIRMED = "confirmed"    # a person agreed this is true / still open
    DONE = "done"              # commitment delivered, question answered, risk closed
    SUPERSEDED = "superseded"  # replaced by a newer event
    REJECTED = "rejected"      # a person said the proposal is wrong


class Role(StrEnum):
    OWNER = "owner"
    MEMBER = "member"
    VIEWER = "viewer"


# Friendly labels shown in the UI. Kept here so wording stays consistent.
EVENT_TYPE_LABELS: dict[str, str] = {
    EventType.DECISION: "Decision",
    EventType.COMMITMENT: "Commitment",
    EventType.CHANGE: "Change",
    EventType.RISK: "Risk",
    EventType.QUESTION: "Open question",
}

# Which status moves a person is allowed to make. Anything not listed is
# refused, so the history stays meaningful (e.g. "rejected" cannot jump to
# "done" without first being confirmed).
ALLOWED_STATUS_CHANGES: dict[str, set[str]] = {
    EventStatus.PROPOSED: {EventStatus.CONFIRMED, EventStatus.REJECTED},
    EventStatus.CONFIRMED: {EventStatus.DONE, EventStatus.SUPERSEDED},
    EventStatus.DONE: {EventStatus.CONFIRMED},          # re-open
    EventStatus.REJECTED: {EventStatus.PROPOSED},       # undo a rejection
    EventStatus.SUPERSEDED: set(),                      # final
}


def can_change_status(current: str, new: str) -> bool:
    """Return True if moving an event from `current` to `new` is allowed."""
    return new in ALLOWED_STATUS_CHANGES.get(current, set())


class ProposedEvent(BaseModel):
    """
    One event suggested by the AI extractor.

    The AI's raw output is untrusted text, so it is validated against this
    model before anything touches the database. Invalid items are dropped,
    not "fixed up" by guessing.
    """

    type: EventType
    summary: str = Field(min_length=3, max_length=300)
    detail: str = Field(default="", max_length=2000)
    owner: str | None = Field(default=None, max_length=120)
    due_date: date | None = None
    evidence: str = Field(default="", max_length=500)  # short quote showing where it came from

    @field_validator("summary", "detail", "evidence", mode="before")
    @classmethod
    def _strip_text(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value

    @field_validator("owner", mode="before")
    @classmethod
    def _empty_owner_is_none(cls, value: object) -> object:
        # Models often return "", "unknown" or "-" when nobody is named.
        if isinstance(value, str) and value.strip().lower() in {"", "unknown", "-", "n/a", "none"}:
            return None
        return value.strip() if isinstance(value, str) else value

    @field_validator("due_date", mode="before")
    @classmethod
    def _bad_date_is_none(cls, value: object) -> object:
        # A vague date like "next week" is not a date. Keep the event, drop the date.
        if isinstance(value, str):
            try:
                return date.fromisoformat(value.strip())
            except ValueError:
                return None
        return value
