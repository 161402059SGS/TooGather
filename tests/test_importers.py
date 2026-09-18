"""
Tests for reading uploaded files.

The WhatsApp tests matter most. An export is the messiest input TooGather
accepts - two locale-dependent formats, wrapped messages, and a third of the
file being media placeholders - and getting it wrong means the extractor reads
noise and proposes nonsense. Each test below pins down one of those hazards.

The Word tests build a .docx in memory rather than committing a binary
fixture, so what is being parsed is visible in the test itself.
"""

import io
import zipfile

import pytest

from toogather import importers
from toogather.importers import office, whatsapp

# ---------------------------------------------------------------------
# Recognising an export
# ---------------------------------------------------------------------

ANDROID_EXPORT = """12/03/2024, 09.14 - Budi Santoso: Mapping file dikirim hari Jumat ya
12/03/2024, 09.15 - Siti: Noted pak
12/03/2024, 09.16 - Budi Santoso: <Media omitted>
12/03/2024, 09.20 - Siti: Kalau gudang Surabaya
belum siap gimana?
12/03/2024, 09.22 - Budi Santoso: Kita pakai batch numbering dulu
"""

IOS_EXPORT = """[12/03/2024, 09.14.22] Budi Santoso: Mapping file dikirim hari Jumat
[12/03/2024, 09.15.01] Siti: Noted pak
[12/03/2024, 09.16.40] Siti: image omitted
"""


def test_android_export_is_recognised():
    assert whatsapp.looks_like_export(ANDROID_EXPORT)


def test_ios_export_is_recognised():
    assert whatsapp.looks_like_export(IOS_EXPORT)


def test_ordinary_meeting_notes_are_not_mistaken_for_an_export():
    notes = """Minutes, 12/03/2024

    Present: Budi, Siti.
    Budi will send the mapping file on Friday.
    Siti raised that the Surabaya warehouse is not ready.
    """
    assert not whatsapp.looks_like_export(notes)


def test_an_empty_file_is_not_an_export():
    assert not whatsapp.looks_like_export("")


# ---------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------


def test_messages_get_author_and_text():
    chat = whatsapp.parse(ANDROID_EXPORT)
    assert chat.messages[0].author == "Budi Santoso"
    assert chat.messages[0].text == "Mapping file dikirim hari Jumat ya"


def test_a_wrapped_message_stays_one_message():
    """A newline inside a message must not look like a second message."""
    chat = whatsapp.parse(ANDROID_EXPORT)
    wrapped = [m for m in chat.messages if m.author == "Siti" and "gudang" in m.text]
    assert len(wrapped) == 1
    assert wrapped[0].text == "Kalau gudang Surabaya\nbelum siap gimana?"


def test_media_placeholders_are_dropped_and_counted():
    chat = whatsapp.parse(ANDROID_EXPORT)
    assert chat.dropped == 1
    assert all("Media omitted" not in m.text for m in chat.messages)


def test_ios_stamps_are_kept_exactly_as_written():
    """
    WhatsApp does not record its own date format, so reformatting would mean
    guessing whether 03/12 is March or December.
    """
    chat = whatsapp.parse(IOS_EXPORT)
    assert chat.messages[0].stamp == "12/03/2024, 09.14.22"


def test_group_notices_are_counted_not_imported():
    export = (
        "12/03/2024, 09.10 - Budi created group \"Proyek Gudang\"\n"
        "12/03/2024, 09.11 - Budi added Siti\n"
        "12/03/2024, 09.14 - Budi: Mulai ya\n"
    )
    chat = whatsapp.parse(export)
    assert chat.notices == 2
    assert len(chat.messages) == 1


def test_transcript_header_warns_about_ambiguous_dates():
    transcript, note = whatsapp.to_transcript(ANDROID_EXPORT)
    assert "do not assume" in transcript
    assert "Budi Santoso" in transcript
    assert "skipped 1 media" in note


# ---------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------


def test_a_whatsapp_txt_is_turned_into_a_transcript():
    result = importers.read_file("chat.txt", ANDROID_EXPORT.encode("utf-8"))
    assert "WhatsApp chat export" in result.text
    assert result.note


def test_an_ordinary_txt_is_passed_through_untouched():
    result = importers.read_file("notes.txt", b"Budi will send the file on Friday.")
    assert result.text == "Budi will send the file on Friday."
    assert result.note == ""


def test_a_byte_order_mark_is_stripped():
    """Windows editors add one, and it would otherwise start the first summary."""
    result = importers.read_file("notes.txt", b"\xef\xbb\xbfDecision: use batch numbers")
    assert result.text.startswith("Decision:")


def test_pasted_text_gets_the_same_whatsapp_treatment():
    result = importers.read_text(ANDROID_EXPORT)
    assert "WhatsApp chat export" in result.text


def test_an_unknown_type_is_refused_with_advice():
    with pytest.raises(importers.UnsupportedFile) as caught:
        importers.read_file("scope.xlsx", b"whatever")
    assert "paste" in str(caught.value)


def test_the_old_doc_format_says_what_to_do_about_it():
    with pytest.raises(importers.UnsupportedFile) as caught:
        importers.read_file("minutes.doc", b"whatever")
    assert ".docx" in str(caught.value)


# ---------------------------------------------------------------------
# Word
# ---------------------------------------------------------------------

_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def _docx(body_xml: str) -> bytes:
    """A minimal .docx holding the given document body."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            "word/document.xml",
            f'<?xml version="1.0"?><w:document xmlns:w="{_W}"><w:body>{body_xml}'
            "</w:body></w:document>",
        )
    return buffer.getvalue()


def test_docx_paragraphs_become_text():
    raw = _docx(
        "<w:p><w:r><w:t>Decision: batch numbering</w:t></w:r></w:p>"
        "<w:p><w:r><w:t>Budi sends the mapping file.</w:t></w:r></w:p>"
    )
    text, note = office.docx_text(raw)
    assert "Decision: batch numbering" in text
    assert "Budi sends the mapping file." in text
    assert "2 paragraphs" in note


def test_docx_runs_inside_one_paragraph_are_joined():
    """Word splits a sentence into runs wherever formatting changes."""
    raw = _docx(
        "<w:p><w:r><w:t>Budi sends the </w:t></w:r>"
        "<w:r><w:t>mapping file</w:t></w:r>"
        "<w:r><w:t> on Friday.</w:t></w:r></w:p>"
    )
    text, _ = office.docx_text(raw)
    assert text == "Budi sends the mapping file on Friday."


def test_docx_table_cells_come_through_in_order():
    raw = _docx(
        "<w:tbl><w:tr>"
        "<w:tc><w:p><w:r><w:t>Budi</w:t></w:r></w:p></w:tc>"
        "<w:tc><w:p><w:r><w:t>Friday</w:t></w:r></w:p></w:tc>"
        "</w:tr></w:tbl>"
    )
    text, _ = office.docx_text(raw)
    assert text.index("Budi") < text.index("Friday")


def test_an_empty_docx_says_so_rather_than_importing_nothing():
    with pytest.raises(office.ImportProblem) as caught:
        office.docx_text(_docx("<w:p></w:p>"))
    assert "no text" in str(caught.value)


def test_a_file_that_is_not_a_zip_is_refused():
    with pytest.raises(office.ImportProblem) as caught:
        office.docx_text(b"this is not a zip file")
    assert ".docx" in str(caught.value)


# ---------------------------------------------------------------------
# PDF
#
# These need pypdf, which the pure-logic tests otherwise do not, so they skip
# where it is absent rather than failing.
# ---------------------------------------------------------------------


@pytest.mark.parametrize("raw", [
    b"%PDF-1.4 not really a pdf",   # right header, truncated body
    b"",                            # empty upload
    b"hello world",                 # not a PDF at all
], ids=["truncated", "empty", "not-a-pdf"])
def test_a_damaged_pdf_gives_a_readable_message(raw):
    """
    Whatever pypdf raises for a damaged file has to come back as an
    ImportProblem. This caught a real bug: the module imported pypdf's error
    base under a name pypdf does not export, so every unreadable PDF was
    reported to the uploader as "pypdf is not installed".
    """
    pytest.importorskip("pypdf")
    with pytest.raises(office.ImportProblem) as caught:
        office.pdf_text(raw)
    assert "not installed" not in str(caught.value)
    assert "could not be read" in str(caught.value) or "no text in it" in str(caught.value)


def test_a_dtd_hidden_behind_a_long_comment_is_still_refused():
    """
    A DTD must precede the root element, but the comments allowed in front of
    it can be any length. Scanning only the first few kilobytes let a crafted
    file walk straight past the check, and ElementTree does expand internal
    entities, so nothing downstream would have caught it.
    """
    padding = "<!-- " + ("x" * 8000) + " -->"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            "word/document.xml",
            '<?xml version="1.0"?>' + padding
            + '<!DOCTYPE lolz [<!ENTITY lol "lol">]>'
            + f'<w:document xmlns:w="{_W}"><w:body/></w:document>',
        )
    with pytest.raises(office.ImportProblem) as caught:
        office.docx_text(buffer.getvalue())
    assert "exhaust the server" in str(caught.value)


def test_a_document_mentioning_entity_in_its_text_is_still_read():
    """
    The DTD scan covers the whole part, so it has to not fire on prose. Word
    escapes a literal "<!ENTITY" as "&lt;!ENTITY", which the raw-byte search
    does not match.
    """
    raw = _docx(
        "<w:p><w:r><w:t>Never write &lt;!ENTITY in a schema.</w:t></w:r></w:p>"
    )
    text, _ = office.docx_text(raw)
    assert "ENTITY" in text


def test_a_docx_that_decompresses_far_beyond_the_limit_is_refused():
    """
    The size a zip declares for an entry is in the archive's own header, so it
    cannot be trusted. The reader has to stop on what it actually reads.
    """
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        # Compresses to a few kilobytes, expands to well past the cap.
        archive.writestr("word/document.xml", b"A" * (office.MAX_DOCX_XML_BYTES + 1024))
    with pytest.raises(office.ImportProblem) as caught:
        office.docx_text(buffer.getvalue())
    assert "too large" in str(caught.value)


def test_a_docx_declaring_a_dtd_is_refused():
    """
    Word never writes a DTD. A file that does is either broken or built to
    make the XML parser expand entities until the process dies.
    """
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            "word/document.xml",
            '<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol">]>'
            f'<w:document xmlns:w="{_W}"><w:body/></w:document>',
        )
    with pytest.raises(office.ImportProblem) as caught:
        office.docx_text(buffer.getvalue())
    assert "exhaust the server" in str(caught.value)
