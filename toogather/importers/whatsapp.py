"""
Reading a WhatsApp chat export.

A great deal of project coordination happens in WhatsApp groups, and the
"Export chat" button produces a .txt file that looks like this:

    12/03/2024, 09.14 - Budi Santoso: Mapping file dikirim hari Jumat ya
    [12/03/2024, 09.14.22] Budi Santoso: Mapping file dikirim hari Jumat

Handing that file to the extractor unchanged works badly: half of it is
"<Media omitted>", join and leave notices, and the encryption banner, and a
message that wraps onto three lines looks like three messages. This module
turns an export into a clean transcript first.

One deliberate non-feature: timestamps are copied through exactly as they
appear. WhatsApp writes dates in the exporting phone's locale and records
nothing about which locale that was, so "03/12" may be 3 December or 12 March.
Reformatting would mean guessing, and a guessed date in a commitment's
deadline is worse than an unparsed one. The extractor is told the same thing
by the header line the transcript starts with.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# WhatsApp sprinkles left-to-right marks through iOS exports.
_INVISIBLE = "‎‏‪‬"

# iOS:      [12/03/2024, 09.14.22] Budi: text
# The stamp is whatever sits inside the brackets; it is validated by the
# date-like test below rather than by trying to match every locale.
_IOS_LINE = re.compile(r"^\[(?P<stamp>[^\]]{6,40})\]\s*(?P<rest>.*)$")

# Android:  12/03/2024, 09.14 - Budi: text
_ANDROID_LINE = re.compile(
    r"""^(?P<stamp>
            \d{1,4}[/.\-]\d{1,2}[/.\-]\d{1,4}   # 12/03/2024, 2024-03-12, 3.12.24
            ,?\s*
            \d{1,2}[:.]\d{2}(?:[:.]\d{2})?      # 09.14, 09:14:22
            (?:\s*[APap]\.?[Mm]\.?)?            # optional am/pm
         )
         \s+-\s+
         (?P<rest>.*)$""",
    re.VERBOSE,
)

_LOOKS_LIKE_STAMP = re.compile(r"\d{1,4}[/.\-]\d{1,2}[/.\-]\d{1,4}")

# A message line is "Author: text". A group notice ("Budi added Siti") has no
# colon, so the split is also how system lines are recognised.
_AUTHOR_SPLIT = re.compile(r"^(?P<author>[^:]{1,80}?):\s(?P<text>.*)$", re.DOTALL)

# Message bodies that carry no information. Matched case-insensitively against
# the whole message, so a real message that merely mentions one is kept.
_NOISE = {
    "<media omitted>",
    "<media tidak disertakan>",
    "image omitted",
    "video omitted",
    "audio omitted",
    "sticker omitted",
    "gif omitted",
    "document omitted",
    "contact card omitted",
    "this message was deleted",
    "you deleted this message",
    "pesan ini telah dihapus",
    "anda menghapus pesan ini",
    "null",
    "missed voice call",
    "missed video call",
}

# The banner WhatsApp puts at the top of every export.
_BANNER_MARKERS = (
    "end-to-end encrypted",
    "terenkripsi secara end-to-end",
)


@dataclass(frozen=True)
class Message:
    stamp: str      # exactly as the export wrote it
    author: str
    text: str


@dataclass(frozen=True)
class Chat:
    messages: tuple[Message, ...]
    notices: int        # group notices: added, removed, subject changed
    dropped: int        # media placeholders and deleted messages


def _clean(line: str) -> str:
    return line.translate({ord(c): None for c in _INVISIBLE}).rstrip()


def _split_stamp(line: str) -> tuple[str, str] | None:
    """Return (stamp, rest) if this line starts a new message, else None."""
    ios = _IOS_LINE.match(line)
    if ios and _LOOKS_LIKE_STAMP.search(ios.group("stamp")):
        return ios.group("stamp").strip(), ios.group("rest")
    android = _ANDROID_LINE.match(line)
    if android:
        return android.group("stamp").strip(), android.group("rest")
    return None


def looks_like_export(text: str, sample_lines: int = 60) -> bool:
    """
    Is this file a WhatsApp export?

    Decided from the first `sample_lines` non-blank lines: if most of them
    start with a timestamp in one of the two export shapes, it is one. A
    meeting transcript that happens to contain one dated line is not.
    """
    checked = starts = 0
    for raw in text.splitlines():
        line = _clean(raw)
        if not line:
            continue
        checked += 1
        if _split_stamp(line) is not None:
            starts += 1
        if checked >= sample_lines:
            break
    return checked >= 3 and starts >= max(3, checked // 2)


def _is_noise(text: str) -> bool:
    lowered = text.strip().lower()
    if lowered in _NOISE:
        return True
    return any(marker in lowered for marker in _BANNER_MARKERS)


def parse(text: str) -> Chat:
    """
    Turn an export into messages.

    Lines that do not start with a timestamp belong to the message above
    them: that is how a message containing a newline is exported.
    """
    messages: list[Message] = []
    pending: list[str] = []     # continuation lines of the message being built
    notices = dropped = 0

    def flush() -> None:
        nonlocal dropped
        if not messages or not pending:
            pending.clear()
            return
        last = messages[-1]
        joined = (last.text + "\n" + "\n".join(pending)).strip()
        pending.clear()
        messages[-1] = Message(last.stamp, last.author, joined)

    for raw in text.splitlines():
        line = _clean(raw)
        split = _split_stamp(line) if line else None

        if split is None:
            if line:
                pending.append(line)
            continue

        flush()
        stamp, rest = split
        author_match = _AUTHOR_SPLIT.match(rest.strip())
        if author_match is None:
            # No "Name:" - a group notice, or the encryption banner.
            notices += 1
            continue

        body = author_match.group("text").strip()
        if _is_noise(body):
            dropped += 1
            continue
        messages.append(Message(stamp, author_match.group("author").strip(), body))

    flush()
    return Chat(tuple(messages), notices, dropped)


def to_transcript(text: str) -> tuple[str, str]:
    """
    Render an export as a transcript, with a one-line note about what happened.

    Returns (transcript, note). The note is shown to whoever uploaded the file,
    so it says what was thrown away rather than silently dropping it.
    """
    chat = parse(text)
    if not chat.messages:
        return text, "This looked like a WhatsApp export but held no messages, so it was kept as-is."

    people = sorted({m.author for m in chat.messages})
    header = (
        f"WhatsApp chat export: {len(chat.messages)} messages "
        f"from {len(people)} people ({', '.join(people[:12])}"
        f"{', …' if len(people) > 12 else ''}).\n"
        f"First: {chat.messages[0].stamp}. Last: {chat.messages[-1].stamp}.\n"
        "Timestamps are copied from the export exactly as WhatsApp wrote them. "
        "The export does not record its date format, so do not assume whether "
        "a date like 03/12 is 3 December or 12 March.\n"
    )
    body = "\n".join(f"[{m.stamp}] {m.author}: {m.text}" for m in chat.messages)

    note_parts = [f"Read {len(chat.messages)} WhatsApp messages from {len(people)} people"]
    if chat.dropped:
        note_parts.append(f"skipped {chat.dropped} media or deleted message(s)")
    if chat.notices:
        note_parts.append(f"skipped {chat.notices} group notice(s)")
    return f"{header}\n{body}\n", "; ".join(note_parts) + "."
