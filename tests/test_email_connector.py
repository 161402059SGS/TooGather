"""
Tests for the email connector's message handling.

No IMAP server is involved: messages are built with the standard library and
run through the same functions the connector uses on a real mailbox. What is
pinned down here is the part that decides what the extractor ends up reading -
which is where a mail connector is usually wrong, because a thread quotes
itself and arrives ten times over.
"""

from email.message import EmailMessage

import pytest

from toogather import connectors
from toogather.connectors.email import (
    EmailConnector,
    message_to_text,
    parse_cursor,
    trim_quoted_reply,
)


def _message(subject, body, sender="Budi <budi@example.com>", html=None):
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = sender
    message["To"] = "warehouse@example.com"
    message["Date"] = "Tue, 12 Mar 2024 09:14:00 +0700"
    if html is None:
        message.set_content(body)
    else:
        message.set_content(body)
        message.add_alternative(html, subtype="html")
    return message


# ---------------------------------------------------------------------
# Turning a message into a note
# ---------------------------------------------------------------------


def test_headers_and_body_end_up_in_the_note():
    title, content = message_to_text(
        _message("Mapping file", "I will send it on Friday."))
    assert title == "Email: Mapping file"
    assert "From: Budi <budi@example.com>" in content
    assert "Subject: Mapping file" in content
    assert "I will send it on Friday." in content


def test_an_encoded_subject_is_decoded():
    """Non-ASCII subjects arrive RFC 2047-encoded."""
    encoded = "=?utf-8?q?Perubahan_jadwal_pengiriman?="
    title, content = message_to_text(_message(encoded, "body"))
    assert "Perubahan jadwal pengiriman" in title
    assert "Perubahan jadwal pengiriman" in content


def test_a_malformed_subject_degrades_instead_of_losing_the_message():
    title, _ = message_to_text(_message("=?utf-8?q?broken", "body"))
    assert title.startswith("Email:")


def test_a_message_with_no_subject_still_has_a_title():
    title, _ = message_to_text(_message(None, "body"))
    assert "(no subject)" in title


def test_an_html_only_message_has_its_tags_stripped():
    message = EmailMessage()
    message["Subject"] = "Scope approved"
    message["From"] = "client@example.com"
    message.set_content(
        "<html><body><p>We <b>approve</b> the scope.</p>"
        "<script>alert(1)</script></body></html>",
        subtype="html",
    )
    _title, content = message_to_text(message)
    assert "We approve the scope." in " ".join(content.split())
    assert "<b>" not in content
    assert "alert(1)" not in content


def test_plain_text_is_preferred_over_the_html_alternative():
    message = _message("Both", "The plain version.", html="<p>The HTML version.</p>")
    _title, content = message_to_text(message)
    assert "The plain version." in content
    assert "HTML version" not in content


def test_a_very_long_message_is_cut_and_says_so():
    _title, content = message_to_text(_message("Long", "x" * 40_000))
    assert "was not imported" in content
    assert len(content) < 25_000


# ---------------------------------------------------------------------
# Quoted replies
#
# Without this a ten-message thread arrives ten times, and the extractor
# proposes the same decision from every copy.
# ---------------------------------------------------------------------


@pytest.mark.parametrize("marker", [
    "On Tue, 12 Mar 2024 at 09:14, Budi <budi@example.com> wrote:",
    "-----Original Message-----",
    "Pada tanggal Sel, 12 Mar 2024 pukul 09.14 Budi menulis:",
    "__________________________________",
])
def test_everything_from_a_quote_marker_onwards_is_dropped(marker):
    body = f"Yes, Friday works.\n\n{marker}\n> the entire previous thread\n> more of it"
    assert trim_quoted_reply(body) == "Yes, Friday works."


def test_a_message_with_no_quote_is_left_alone():
    body = "Just one message.\n\nWith two paragraphs."
    assert trim_quoted_reply(body) == body


def test_trimming_never_returns_leading_or_trailing_space():
    assert trim_quoted_reply("\n\n  Hello.  \n\n") == "Hello."


# ---------------------------------------------------------------------
# The cursor
# ---------------------------------------------------------------------


def test_a_cursor_round_trips():
    assert parse_cursor("123456:42") == ("123456", 42)


@pytest.mark.parametrize("bad", ["", "nonsense", "123456:", ":", "123456:abc"])
def test_an_unreadable_cursor_means_start_fresh(bad):
    """Anything unparseable has to mean "no cursor", never an exception."""
    assert parse_cursor(bad) == ("", 0) or parse_cursor(bad)[1] == 0


# ---------------------------------------------------------------------
# Setup validation
# ---------------------------------------------------------------------


def test_a_complete_setup_is_accepted():
    good = {"host": "imap.example.com", "port": "993",
            "username": "warehouse@example.com", "mailbox": "INBOX"}
    assert EmailConnector().config_problem(good) is None


def test_the_port_has_to_be_a_number():
    bad = {"host": "imap.example.com", "port": "abc", "username": "a@b.c"}
    assert "number" in EmailConnector().config_problem(bad)


def test_an_out_of_range_port_is_refused():
    bad = {"host": "imap.example.com", "port": "99999", "username": "a@b.c"}
    assert EmailConnector().config_problem(bad) is not None


def test_a_missing_host_is_refused():
    assert EmailConnector().config_problem({"username": "a@b.c"}) is not None


@pytest.mark.parametrize("bad_name", [
    'INBOX"',                 # would close the quoted string in SELECT "..."
    "INBOX\r\nLOGOUT",        # a second IMAP command smuggled in
    "INBOX\ta",               # any other control character
    "x" * 101,                # longer than the field allows
])
def test_a_folder_name_cannot_carry_anything_into_the_protocol(bad_name):
    """
    The mailbox name goes into an IMAP command inside quotes, so it is kept to
    characters that cannot end the quoting or start a second command.

    Surrounding whitespace is not tested here because it never reaches this
    check: the form reader strips it, and a trailing newline on an otherwise
    ordinary name leaves an ordinary name.
    """
    problem = EmailConnector().config_problem({
        "host": "imap.example.com", "username": "a@b.c", "mailbox": bad_name})
    assert problem is not None, bad_name


def test_ordinary_folder_names_are_accepted():
    for name in ["INBOX", "Clients/Warehouse", "Proyek Gudang", "archive.2024"]:
        assert EmailConnector().config_problem({
            "host": "imap.example.com", "username": "a@b.c", "mailbox": name}) is None


def test_email_is_registered_as_a_polling_connector():
    connector = connectors.get("email")
    assert connector is not None
    assert connector.inbound is False
