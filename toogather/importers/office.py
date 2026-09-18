"""
Pulling plain text out of Word and PDF files.

People are given a .docx of the minutes and a .pdf of the signed scope, and
telling them to "copy the text and paste it instead" is how a tool gets used
once and then forgotten. Both formats are read here and handed on as text;
everything downstream treats them exactly like a pasted note.

Neither reader tries to preserve layout. TooGather wants sentences, because
that is what the extractor reads and what a person searches. Tables come
through as rows of text, which is enough to keep "Budi | mapping file |
Friday" readable on one line.

Both readers are defensive, because a file can arrive from anywhere:

  * A .docx part is read through a capped stream rather than in one go. The
    size a zip declares for an entry is in the archive's own header and is
    therefore attacker-controlled, so a small file can claim to be small and
    still decompress to gigabytes.
  * A .docx whose XML declares a DTD is refused, and the whole part is
    scanned for one. `xml.etree.ElementTree` does expand internal entities,
    so this check is the only thing standing between a crafted file and a
    process that grows until it is killed.
  * A PDF is read page by page with a page cap, and a PDF with no text layer
    is reported as a scan rather than silently imported as an empty note.
"""

from __future__ import annotations

import io
import re
import zipfile
from xml.etree import ElementTree

# Word's main namespace. Every element below is in it.
_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

MAX_DOCX_XML_BYTES = 40 * 1024 * 1024    # uncompressed; a 40 MB document.xml is already absurd
MAX_PDF_PAGES = 400

_DOCTYPE = re.compile(rb"<!(?:DOCTYPE|ENTITY)", re.IGNORECASE)


class ImportProblem(Exception):
    """The file could not be read. The message is shown to whoever uploaded it."""


# ---------------------------------------------------------------------
# Word (.docx)
# ---------------------------------------------------------------------


def _paragraph_text(paragraph: ElementTree.Element) -> str:
    """
    The visible text of one Word paragraph.

    `w:t` holds the text; `w:tab` and `w:br` are formatting elements that carry
    no text of their own but do separate words, so they become whitespace.
    Iterating the whole subtree keeps everything in reading order, including
    text inside hyperlinks and tracked-change runs.
    """
    pieces: list[str] = []
    for node in paragraph.iter():
        if node.tag == f"{_W}t":
            pieces.append(node.text or "")
        elif node.tag == f"{_W}tab":
            pieces.append("\t")
        elif node.tag == f"{_W}br":
            pieces.append("\n")
    return "".join(pieces).strip()


def docx_text(raw: bytes) -> tuple[str, str]:
    """
    Read a .docx. Returns (text, note).

    A .docx is a zip file; the document body is one XML part inside it. Tables
    need no special handling: their cells contain ordinary paragraphs, and
    iterating paragraphs in document order picks them up in place.
    """
    try:
        archive = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile as exc:
        raise ImportProblem(
            "That file is not a readable .docx. If it was saved as the older .doc "
            "format, open it in Word and use Save As to make a .docx."
        ) from exc

    with archive:
        try:
            entry = archive.open("word/document.xml")
        except KeyError as exc:
            raise ImportProblem(
                "That .docx has no document body. It may be a template or a corrupt file."
            ) from exc
        # Read through the stream with a cap rather than trusting the size the
        # zip declares. That number lives in the archive's own header, so a
        # crafted file can claim to be small and still decompress to gigabytes;
        # asking for one byte past the limit stops it without allocating them.
        with entry:
            xml = entry.read(MAX_DOCX_XML_BYTES + 1)

    if len(xml) > MAX_DOCX_XML_BYTES:
        raise ImportProblem("That document is too large to read. Split it into parts.")

    # The whole part is scanned, not just its beginning. A DTD has to appear
    # before the root element, but the comments allowed in front of it can be
    # any length, so a fixed window is trivially stepped over. Scanning
    # everything cannot raise a false alarm on real text either: Word escapes
    # a literal "<!ENTITY" in a paragraph as "&lt;!ENTITY", so the raw bytes
    # only occur in an actual declaration.
    if _DOCTYPE.search(xml):
        raise ImportProblem(
            "That .docx declares an XML document type, which Word does not write. "
            "It was not read, because such a file can be built to exhaust the server."
        )

    try:
        body = ElementTree.fromstring(xml)
    except ElementTree.ParseError as exc:
        raise ImportProblem(f"That .docx could not be parsed: {exc}") from exc

    paragraphs = [_paragraph_text(p) for p in body.iter(f"{_W}p")]
    kept = [p for p in paragraphs if p]
    if not kept:
        raise ImportProblem(
            "That Word file has no text in it. If the content is an image or a scan, "
            "it needs to be typed or run through OCR first."
        )
    return "\n\n".join(kept), f"Read {len(kept)} paragraphs from the Word document."


# ---------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------


def pdf_text(raw: bytes) -> tuple[str, str]:
    """
    Read a .pdf. Returns (text, note).

    A PDF that holds only scanned images has no text layer, and extraction
    returns empty strings for every page. That is reported plainly rather than
    creating an empty note that looks like it worked.
    """
    # Only the reader is imported. Naming pypdf's exception base here as well
    # would tie this message to a class name that has been renamed before, and
    # getting that wrong reports a damaged file as "pypdf is not installed".
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover - only when pypdf is not installed
        raise ImportProblem(
            "PDF reading needs the pypdf package, which is not installed on this server."
        ) from exc

    try:
        reader = PdfReader(io.BytesIO(raw))
        # An empty password opens the very common "printing restricted" case;
        # decrypt returning 0 means even that did not work.
        if reader.is_encrypted and reader.decrypt("") == 0:
            raise ImportProblem(
                "That PDF is password-protected. Remove the password and upload it again."
            )
        pages = reader.pages[:MAX_PDF_PAGES]
        total_pages = len(reader.pages)
        texts = [(page.extract_text() or "").strip() for page in pages]
    except ImportProblem:
        raise
    except Exception as exc:
        # A damaged PDF usually raises one of pypdf's own errors, but a
        # sufficiently malformed one surfaces whatever the parser hit on the
        # way down - a struct error, a recursion limit. The answer to the
        # person is the same either way, and an upload form must not 500.
        raise ImportProblem(f"That PDF could not be read: {exc}") from exc

    kept = [t for t in texts if t]
    if not kept:
        raise ImportProblem(
            "That PDF has no text in it, which usually means it is a scan. "
            "Run it through OCR first, or paste the text instead."
        )

    note = f"Read {len(kept)} pages of text from the PDF."
    if total_pages > MAX_PDF_PAGES:
        note += f" Only the first {MAX_PDF_PAGES} of {total_pages} pages were read."
    return "\n\n".join(kept), note
