"""
Tests for the webhook connector.

`parse_delivery` is the only code in TooGather that reads a body posted by an
unauthenticated stranger, so these are the tests that matter most in the file:
every shape of rubbish has to come back as a ConnectorError with a readable
message, never as an exception that becomes a 500.
"""

import json

import pytest

from toogather import connectors
from toogather.connectors.base import Delivery
from toogather.connectors.webhook import (
    MAX_BODY_BYTES,
    WebhookConnector,
    parse_delivery,
)

JSON = "application/json"
TEXT = "text/plain"


# ---------------------------------------------------------------------
# The documented shape
# ---------------------------------------------------------------------


def test_title_and_content_are_used_as_given():
    body = json.dumps({"title": "Deploy 1.4.2", "content": "Shipped to production."})
    item = parse_delivery(JSON, body.encode(), "fallback")
    assert item.title == "Deploy 1.4.2"
    assert item.content == "Shipped to production."


def test_a_missing_title_falls_back_to_the_connector_default():
    body = json.dumps({"content": "Shipped."})
    assert parse_delivery(JSON, body.encode(), "Posted by CI").title == "Posted by CI"


def test_a_blank_title_falls_back_too():
    body = json.dumps({"title": "   ", "content": "Shipped."})
    assert parse_delivery(JSON, body.encode(), "Posted by CI").title == "Posted by CI"


# ---------------------------------------------------------------------
# Whatever else a caller sends
# ---------------------------------------------------------------------


def test_arbitrary_json_is_written_out_readably():
    """
    A caller posting its own structure should not have it turned into a wall of
    braces: the extractor reads this, and so does a person.
    """
    body = json.dumps({
        "event": "deployment",
        "environment": "production",
        "released_by": "Budi",
        "changes": ["batch numbering", "stock aging report"],
    })
    item = parse_delivery(JSON, body.encode(), "fallback")
    assert "environment: production" in item.content
    assert "released_by: Budi" in item.content
    assert "- batch numbering" in item.content
    assert "{" not in item.content


def test_a_title_is_picked_from_a_recognisable_key():
    for key in ("title", "subject", "name", "summary"):
        body = json.dumps({key: "Release 2.0", "detail": "..."})
        assert parse_delivery(JSON, body.encode(), "fallback").title == "Release 2.0"


def test_nested_json_is_indented_rather_than_flattened():
    body = json.dumps({"build": {"number": 42, "status": "passed"}})
    content = parse_delivery(JSON, body.encode(), "fallback").content
    assert "build:" in content
    assert "  number: 42" in content


def test_booleans_read_as_words():
    body = json.dumps({"passed": True, "flaky": False})
    content = parse_delivery(JSON, body.encode(), "fallback").content
    assert "passed: yes" in content
    assert "flaky: no" in content


def test_plain_text_is_taken_as_the_note():
    item = parse_delivery(TEXT, b"The client approved the scope today.", "From the script")
    assert item.content == "The client approved the scope today."
    assert item.title == "From the script"


def test_a_body_with_no_content_type_is_treated_as_text():
    item = parse_delivery("", b"just words", "fallback")
    assert item.content == "just words"


# ---------------------------------------------------------------------
# Rubbish
# ---------------------------------------------------------------------


def test_an_empty_body_is_refused():
    with pytest.raises(connectors.ConnectorError) as caught:
        parse_delivery(TEXT, b"", "fallback")
    assert "empty" in str(caught.value)


def test_whitespace_only_is_refused():
    with pytest.raises(connectors.ConnectorError):
        parse_delivery(TEXT, b"   \n\t  ", "fallback")


def test_broken_json_says_so_rather_than_raising():
    with pytest.raises(connectors.ConnectorError) as caught:
        parse_delivery(JSON, b'{"title": "unterminated', "fallback")
    assert "could not be read" in str(caught.value)


def test_an_oversized_body_is_refused_with_advice():
    with pytest.raises(connectors.ConnectorError) as caught:
        parse_delivery(TEXT, b"x" * (MAX_BODY_BYTES + 1), "fallback")
    assert "summary" in str(caught.value)


def test_json_that_renders_to_nothing_is_refused():
    with pytest.raises(connectors.ConnectorError) as caught:
        parse_delivery(JSON, b"{}", "fallback")
    assert "nothing to record" in str(caught.value)


def test_invalid_utf8_does_not_raise():
    """A caller sending bytes in some other encoding gets replacement characters."""
    item = parse_delivery(TEXT, b"caf\xe9 meeting notes", "fallback")
    assert "meeting notes" in item.content


def test_a_very_long_title_is_cut():
    body = json.dumps({"title": "x" * 500, "content": "..."})
    assert len(parse_delivery(JSON, body.encode(), "fallback").title) <= 200


# ---------------------------------------------------------------------
# How it fits the framework
# ---------------------------------------------------------------------


def test_the_webhook_is_registered_and_inbound():
    connector = connectors.get("webhook")
    assert connector is not None
    assert connector.inbound is True


def test_an_inbound_connector_is_never_put_on_the_schedule():
    assert connectors.pollable("webhook") is False
    assert connectors.pollable("git") is True
    assert connectors.pollable("email") is True
    assert connectors.pollable("nothing-like-this") is False


def test_asking_a_webhook_to_poll_says_what_is_wrong():
    with pytest.raises(connectors.ConnectorError) as caught:
        WebhookConnector().fetch(connectors.Context(connector_id="c", project_id="p"))
    assert "schedule" in str(caught.value)


def test_posting_to_a_polling_connector_says_what_is_wrong():
    from toogather.connectors.git import GitConnector

    with pytest.raises(connectors.ConnectorError) as caught:
        GitConnector().receive(Delivery(content_type=TEXT, body=b"hello"))
    assert "cannot be posted to" in str(caught.value)


def test_receive_returns_one_item_and_a_note():
    result = WebhookConnector().receive(Delivery(
        content_type=JSON,
        body=json.dumps({"title": "Deploy", "content": "done"}).encode(),
        config={"default_title": "Posted by CI"},
    ))
    assert len(result.items) == 1
    assert result.items[0].title == "Deploy"
    # No external_id: the same summary may legitimately be posted twice.
    assert result.items[0].external_id == ""
    assert "Deploy" in result.note
