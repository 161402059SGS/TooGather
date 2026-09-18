"""
Turning an uploaded file into text.

Everything TooGather understands is text: the extractor reads text, search
indexes text, and a person reviewing a proposal reads text. An importer's only
job is to get from bytes on a form post to that text, and to say in one line
what it did, so the person who uploaded the file can see whether the right
thing was read out of it.

One function does the dispatch: `read_file(filename, raw)`. Adding a format
means adding a branch here and a reader beside it. The web app does not know
which formats exist; it asks this module.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from toogather.importers import office, whatsapp
from toogather.importers.office import ImportProblem

__all__ = ["ImportProblem", "Imported", "UnsupportedFile", "read_file", "accepted_suffixes"]


class UnsupportedFile(Exception):
    """The file type is not one TooGather reads. The message is shown to the uploader."""


@dataclass(frozen=True)
class Imported:
    text: str
    note: str    # one line describing what was read, shown as a flash message


# Text formats that are already text. utf-8-sig strips the byte-order mark
# that Windows editors add, which would otherwise become a stray character at
# the start of the first event's summary.
PLAIN_SUFFIXES = (".txt", ".md", ".markdown", ".vtt", ".srt", ".csv", ".log")

# What the upload form offers. Order is the order shown.
ACCEPTED_SUFFIXES = (*PLAIN_SUFFIXES, ".docx", ".pdf")


def accepted_suffixes() -> tuple[str, ...]:
    return ACCEPTED_SUFFIXES


def _plain_text(raw: bytes) -> str:
    return raw.decode("utf-8-sig", errors="replace")


def read_text(text: str) -> Imported:
    """
    Read text that was pasted rather than uploaded.

    Pasting a WhatsApp export into the box is at least as common as attaching
    the file, so the same recognition runs on both paths.
    """
    if whatsapp.looks_like_export(text):
        transcript, note = whatsapp.to_transcript(text)
        return Imported(transcript, note)
    return Imported(text, "")


def read_file(filename: str, raw: bytes) -> Imported:
    """
    Read an uploaded file into text.

    Raises UnsupportedFile for a format TooGather does not read, and
    ImportProblem when a supported format could not be read - a scanned PDF,
    a corrupt archive. Both messages are written for the person who uploaded
    the file, not for a log.
    """
    suffix = Path(filename).suffix.lower()

    if suffix in PLAIN_SUFFIXES:
        text = _plain_text(raw)
        # A WhatsApp export arrives as an ordinary .txt, so it is recognised by
        # its contents rather than its name.
        if whatsapp.looks_like_export(text):
            transcript, note = whatsapp.to_transcript(text)
            return Imported(transcript, note)
        return Imported(text, "")

    if suffix == ".docx":
        text, note = office.docx_text(raw)
        return Imported(text, note)

    if suffix == ".pdf":
        text, note = office.pdf_text(raw)
        return Imported(text, note)

    if suffix == ".doc":
        raise UnsupportedFile(
            "That is the older Word .doc format, which cannot be read directly. "
            "Open it in Word and use Save As to make a .docx."
        )

    raise UnsupportedFile(
        "TooGather reads " + ", ".join(ACCEPTED_SUFFIXES) + ". "
        "For anything else, copy the text and paste it instead."
    )
