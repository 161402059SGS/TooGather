"""Tests for parsing and validating AI output. No AI service is called."""

from datetime import date

import pytest

from toogather.extraction import ExtractionError, parse_model_output, split_into_chunks
from toogather.models import can_change_status


def test_parses_json_inside_code_fences():
    raw = '```json\n{"events": [{"type": "decision", "summary": "Use batch numbering for FG"}]}\n```'
    proposals = parse_model_output(raw)
    assert len(proposals) == 1
    assert proposals[0].summary == "Use batch numbering for FG"


def test_invalid_items_are_skipped_not_fatal():
    raw = """{"events": [
        {"type": "decision", "summary": "Valid one"},
        {"type": "gossip", "summary": "Unknown type"},
        {"type": "risk", "summary": ""}
    ]}"""
    assert [p.summary for p in parse_model_output(raw)] == ["Valid one"]


def test_vague_owner_and_date_are_cleaned():
    raw = (
        '{"events": [{"type": "commitment", "summary": "Kirim file mapping", '
        '"owner": "unknown", "due_date": "minggu depan"}]}'
    )
    proposal = parse_model_output(raw)[0]
    assert proposal.owner is None
    assert proposal.due_date is None


def test_exact_date_is_kept():
    raw = '{"events": [{"type": "commitment", "summary": "Send file", "due_date": "2026-09-30"}]}'
    assert parse_model_output(raw)[0].due_date == date(2026, 9, 30)


def test_non_json_output_raises_readable_error():
    with pytest.raises(ExtractionError):
        parse_model_output("Sorry, I cannot help with that.")


def test_chunks_respect_limit_and_keep_all_text():
    text = "\n\n".join(f"Paragraph {i} " + "x" * 50 for i in range(40))
    chunks = split_into_chunks(text, max_chars=300)
    assert all(len(c) <= 300 for c in chunks)
    assert "".join(chunks).replace("\n", "") == text.replace("\n", "")


def test_status_changes_follow_the_allowed_paths():
    assert can_change_status("proposed", "confirmed")
    assert can_change_status("confirmed", "done")
    assert not can_change_status("rejected", "done")
    assert not can_change_status("superseded", "confirmed")
