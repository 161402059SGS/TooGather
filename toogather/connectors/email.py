"""
The email connector: bring messages from a mailbox in as material to review.

The intended setup is a mailbox that exists for one project - `warehouse@` or
a `+warehouse` alias - that the team forwards client mail to. Each message
becomes a note carrying who sent it, when, the subject and the body, and from
there it goes through the same path as an upload: proposed events if the
project allows AI, a person confirming what is true.

Why IMAP rather than a provider API: every mail service speaks it, including
the company Exchange server that will never issue anyone an OAuth client. The
cost is that it needs a password, which is why the field is stored encrypted
and why the help text asks for an app password rather than the real one.

TLS is not optional. `imaplib.IMAP4_SSL` is the only client used here, so a
mailbox password never crosses the network in the clear. A server that only
offers plaintext IMAP is refused rather than accommodated.

Where the cursor comes from
---------------------------

IMAP gives each message a UID that is stable within a mailbox, and the mailbox
a UIDVALIDITY that changes if the server ever re-issues those UIDs. The cursor
is `UIDVALIDITY:UID`, so if the mailbox is recreated the connector notices the
validity changed and starts again instead of silently skipping everything.
"""

from __future__ import annotations

import contextlib
import email
import imaplib
import logging
import re
from email.header import decode_header, make_header
from email.message import Message

from toogather.connectors.base import (
    ConfigField,
    Connector,
    ConnectorError,
    Context,
    FetchResult,
    Item,
)

log = logging.getLogger("toogather.connectors.email")

# One run brings in at most this many messages and leaves the rest for the
# next check, so pointing this at a mailbox with ten years of history does not
# produce ten years of notes in one go.
MAX_MESSAGES_PER_RUN = 50

# A single message longer than this is cut. Mail threads quote themselves
# forwards; past this point it is the same text again.
MAX_BODY_CHARS = 20_000

IMAP_TIMEOUT_MARGIN = 10          # leave room for the connector timeout to win

# Quoted-reply markers. Everything from one of these onwards is the previous
# message in the thread, which is already in the project if it mattered.
_QUOTED = re.compile(
    r"^\s*(?:On .{0,120}wrote:|-{2,}\s*Original Message\s*-{2,}|_{10,}|"
    r"From:\s.+\bSent:|Pada .{0,120}menulis:)",
    re.MULTILINE,
)


def _decoded(value: str | None) -> str:
    """
    Turn a raw header into readable text.

    Subjects arrive RFC 2047-encoded (`=?utf-8?B?...?=`) and a malformed one
    raises rather than returning something usable, so a bad header degrades to
    the raw bytes instead of losing the whole message.
    """
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value))).strip()
    except (UnicodeDecodeError, LookupError, ValueError):
        return value.strip()


def _body_text(message: Message) -> str:
    """
    The readable text of a message.

    text/plain is preferred; an HTML-only message has its tags stripped rather
    than being skipped, because plenty of real mail is HTML-only and half a
    message is better than none. Attachments are ignored: this connector reads
    correspondence, and a person can upload a document if one matters.
    """
    plain: list[str] = []
    html: list[str] = []

    for part in message.walk() if message.is_multipart() else [message]:
        if part.get_content_maintype() == "multipart":
            continue
        if part.get_filename():
            continue                      # an attachment, not the message
        content_type = part.get_content_type()
        if content_type not in ("text/plain", "text/html"):
            continue
        try:
            payload = part.get_payload(decode=True)
        except Exception:                 # a malformed part must not lose the rest
            continue
        if not payload:
            continue
        charset = part.get_content_charset() or "utf-8"
        try:
            text = payload.decode(charset, errors="replace")
        except LookupError:
            text = payload.decode("utf-8", errors="replace")
        (plain if content_type == "text/plain" else html).append(text)

    if plain:
        return "\n".join(plain)
    if html:
        # Deliberately crude: drop script and style wholesale, then tags. The
        # result is read by a person and by the extractor, neither of which
        # needs markup, and it is escaped again before it reaches a page.
        stripped = re.sub(r"(?is)<(script|style).*?</\1>", " ", "\n".join(html))
        stripped = re.sub(r"(?s)<[^>]+>", " ", stripped)
        return re.sub(r"[ \t]{2,}", " ", stripped)
    return ""


def trim_quoted_reply(text: str) -> str:
    """
    Cut a message at the point where it starts quoting the one before it.

    Without this, a ten-message thread arrives ten times, and the extractor
    proposes the same decision from each copy.
    """
    match = _QUOTED.search(text)
    trimmed = text[:match.start()] if match else text
    return trimmed.strip()


def message_to_text(message: Message) -> tuple[str, str]:
    """Render one message as (title, content)."""
    subject = _decoded(message.get("Subject")) or "(no subject)"
    sender = _decoded(message.get("From"))
    to = _decoded(message.get("To"))
    date = _decoded(message.get("Date"))

    body = trim_quoted_reply(_body_text(message))
    if len(body) > MAX_BODY_CHARS:
        body = body[:MAX_BODY_CHARS] + "\n\n[the rest of this message was not imported]"

    content = (
        f"Email message.\n"
        f"From: {sender}\n"
        f"To: {to}\n"
        f"Date: {date}\n"
        f"Subject: {subject}\n"
        f"\n{body}\n"
    )
    return f"Email: {subject}"[:200], content


def parse_cursor(cursor: str) -> tuple[str, int]:
    """Split `UIDVALIDITY:UID`. Anything unreadable means "start from now"."""
    validity, _, uid = (cursor or "").partition(":")
    try:
        return validity, int(uid)
    except ValueError:
        return "", 0


class EmailConnector(Connector):
    kind = "email"
    label = "Email mailbox"
    description = (
        "Check an IMAP mailbox for new messages and bring them in as material "
        "to review. Point it at a mailbox or alias the team forwards this "
        "project's mail to, not at somebody's personal inbox."
    )
    fields = (
        ConfigField(
            "host", "IMAP server", placeholder="imap.gmail.com",
            help="TLS is required. A server offering only plaintext IMAP is refused.",
        ),
        ConfigField(
            "port", "Port", placeholder="993", required=False,
            help="Leave empty for 993, the standard IMAP-over-TLS port.",
        ),
        ConfigField(
            "username", "Username", placeholder="warehouse@example.com",
        ),
        ConfigField(
            "mailbox", "Folder", placeholder="INBOX", required=False,
            help="Leave empty for INBOX.",
        ),
        ConfigField(
            "password", "Password", required=True, secret=True,
            help="Use an app password, never the account's real one. "
                 "Stored encrypted; never shown again.",
        ),
    )

    # -- setup -------------------------------------------------------

    def config_problem(self, config: dict) -> str | None:
        problem = super().config_problem(config)
        if problem:
            return problem
        port = str(config.get("port", "")).strip()
        if port:
            try:
                number = int(port)
            except ValueError:
                return "The port has to be a number."
            if not 1 <= number <= 65535:
                return "That is not a valid port number."
        mailbox = str(config.get("mailbox", "")).strip()
        # The mailbox name goes into an IMAP command, so keep it to something
        # that cannot carry a quote or a line break into the protocol.
        if mailbox and not re.fullmatch(r"[A-Za-z0-9 ._/\-]{1,100}", mailbox):
            return "That folder name has characters IMAP does not allow."
        return None

    # -- the run itself ----------------------------------------------

    def _connect(self, ctx: Context) -> imaplib.IMAP4_SSL:
        host = str(ctx.config.get("host", "")).strip()
        port = int(str(ctx.config.get("port", "")).strip() or 993)
        try:
            client = imaplib.IMAP4_SSL(
                host, port,
                timeout=max(5, ctx.timeout_seconds - IMAP_TIMEOUT_MARGIN),
            )
        except (OSError, imaplib.IMAP4.error) as exc:
            raise ConnectorError(f"Could not reach {host}: {exc}") from exc

        try:
            client.login(str(ctx.config.get("username", "")).strip(), ctx.secret)
        except imaplib.IMAP4.error as exc:
            # The server's rejection can echo the username; it never contains
            # the password, but keep it short anyway.
            raise ConnectorError(
                f"The mailbox refused those credentials: {str(exc)[:150]}"
            ) from exc
        return client

    def fetch(self, ctx: Context) -> FetchResult:
        if not ctx.secret:
            raise ConnectorError("This mailbox has no password saved. Enter one and save.")

        mailbox = str(ctx.config.get("mailbox", "")).strip() or "INBOX"
        client = self._connect(ctx)
        try:
            status, data = client.select(f'"{mailbox}"', readonly=True)
            if status != "OK":
                raise ConnectorError(f"The mailbox has no folder called {mailbox}.")

            validity = self._uid_validity(client)
            known_validity, last_uid = parse_cursor(ctx.cursor)
            reset = bool(known_validity) and known_validity != validity
            if reset:
                last_uid = 0

            uids = self._uids_after(client, last_uid)
            if not uids:
                return FetchResult(note=f"No new messages in {mailbox}.")

            batch = uids[:MAX_MESSAGES_PER_RUN]
            items = tuple(self._one_message(client, validity, uid) for uid in batch)
            items = tuple(item for item in items if item is not None)

            note = f"Brought in {len(items)} message(s) from {mailbox}."
            if reset:
                note += (" The mailbox was recreated on the server, so this started "
                         "from its newest messages.")
            if len(uids) > len(batch):
                note += f" {len(uids) - len(batch)} more are waiting for the next check."
            return FetchResult(items=items, cursor=f"{validity}:{batch[-1]}", note=note)
        finally:
            self._close(client)

    # -- IMAP details ------------------------------------------------

    @staticmethod
    def _close(client: imaplib.IMAP4_SSL) -> None:
        """Log out without letting a failing logout mask a real error."""
        with contextlib.suppress(Exception):
            client.logout()

    @staticmethod
    def _uid_validity(client: imaplib.IMAP4_SSL) -> str:
        """
        The mailbox's UIDVALIDITY, reported by SELECT as an untagged response.

        A server that does not send one gets "0", which simply means the
        cursor cannot detect a mailbox being recreated - the UIDs still work.
        """
        raw = client.untagged_responses.get("UIDVALIDITY")
        if not raw:
            return "0"
        value = raw[0]
        return value.decode() if isinstance(value, bytes) else str(value)

    @staticmethod
    def _uids_after(client: imaplib.IMAP4_SSL, last_uid: int) -> list[int]:
        """
        Message UIDs newer than the cursor, oldest first.

        A first run takes the newest messages rather than the whole mailbox:
        `last_uid` of 0 means there is no cursor yet.
        """
        criterion = f"UID {last_uid + 1}:*" if last_uid else "ALL"
        status, data = client.uid("SEARCH", None, criterion)
        if status != "OK" or not data:
            raise ConnectorError("The mailbox refused a search for new messages.")
        uids = sorted(int(part) for part in (data[0] or b"").split())
        # "UID n:*" always returns at least the newest message even when
        # nothing is newer than n, so drop anything already seen.
        uids = [uid for uid in uids if uid > last_uid]
        return uids[-MAX_MESSAGES_PER_RUN:] if not last_uid else uids

    def _one_message(self, client: imaplib.IMAP4_SSL, validity: str, uid: int) -> Item | None:
        status, data = client.uid("FETCH", str(uid), "(RFC822)")
        if status != "OK" or not data or not isinstance(data[0], tuple):
            log.warning("Could not read message UID %s; skipping it", uid)
            return None
        try:
            message = email.message_from_bytes(data[0][1])
            title, content = message_to_text(message)
        except Exception:      # noqa: BLE001 - one bad message must not lose the batch
            log.exception("Could not parse message UID %s; skipping it", uid)
            return None
        return Item(external_id=f"{validity}:{uid}", title=title, content=content)
