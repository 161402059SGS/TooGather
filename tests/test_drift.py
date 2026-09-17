"""Tests for drift rules. These are pure functions, so no database is needed."""

from datetime import date, datetime

from toogather.drift import build_brief, detect_drift

TODAY = date(2026, 9, 17)


def event(**overrides):
    """A confirmed decision by default; override only what a test cares about."""
    base = {
        "id": "00000000-0000-0000-0000-000000000001",
        "type": "decision",
        "status": "confirmed",
        "summary": "Example",
        "owner": None,
        "due_date": None,
        "related_id": None,
        "source_ref": "",
        "created_at": datetime(2026, 9, 10, 9, 0),
    }
    return {**base, **overrides}


def rules_found(events):
    return [f.rule for f in detect_drift(events, TODAY)]


def test_overdue_commitment_is_flagged():
    assert rules_found([event(type="commitment", due_date=date(2026, 9, 15))]) == ["overdue_commitment"]


def test_commitment_due_today_is_not_overdue():
    assert rules_found([event(type="commitment", due_date=TODAY)]) == []


def test_done_commitment_is_not_flagged_even_if_past_due():
    assert rules_found([event(type="commitment", status="done", due_date=date(2026, 9, 1))]) == []


def test_change_without_linked_decision_is_flagged():
    assert rules_found([event(type="change")]) == ["unlinked_change"]


def test_change_linked_to_decision_is_not_flagged():
    assert rules_found([event(type="change", related_id="some-decision")]) == []


def test_risk_without_owner_is_flagged_but_not_with_owner():
    assert rules_found([event(type="risk")]) == ["risk_without_owner"]
    assert rules_found([event(type="risk", owner="Budi")]) == []


def test_question_becomes_stale_after_threshold():
    fresh = event(type="question", created_at=datetime(2026, 9, 10))
    stale = event(type="question", created_at=datetime(2026, 8, 20))
    assert rules_found([fresh]) == []
    assert rules_found([stale]) == ["stale_question"]


def test_unreviewed_proposal_becomes_stale():
    assert rules_found([event(status="proposed", created_at=datetime(2026, 9, 1))]) == ["stale_proposal"]


def test_high_severity_findings_come_first():
    events = [
        event(type="risk"),                                             # medium
        event(type="commitment", due_date=date(2026, 9, 1)),            # high
    ]
    findings = detect_drift(events, TODAY)
    assert [f.severity for f in findings] == ["high", "medium"]


def test_brief_groups_open_items():
    events = [
        event(type="commitment", summary="Send mapping file", owner="Budi", due_date=date(2026, 9, 20)),
        event(type="decision", summary="Use batch numbering"),
        event(type="question", status="done", summary="Answered already"),
        event(status="proposed", summary="Waiting"),
    ]
    brief = build_brief(events, detect_drift(events, TODAY))
    assert [c["summary"] for c in brief["open_commitments"]] == ["Send mapping file"]
    assert brief["open_questions"] == []
    assert brief["awaiting_review"] == 1
