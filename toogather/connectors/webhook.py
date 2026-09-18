"""
The webhook connector: let something else post material into a project.

This is the connector that makes the others optional. Anything that can make
an HTTP request - a CI pipeline, a GitHub Action, Jira's own webhooks, a shell
script after a deploy, an automation tool - can post a note to a project
without TooGather needing to know what it is.

It is the one connector that is *told* rather than *asks*, so it implements
`receive` instead of `fetch` and is never scheduled.

What it accepts
---------------

A POST with a body, in whichever of three shapes the caller finds easiest:

    JSON   {"title": "Deploy 1.4.2", "content": "..."}
    JSON   anything else - the whole document is written out as readable text
    text   the body itself, with the title taken from the connector's settings

Nothing about the request is trusted beyond the secret in its URL. The code
says the caller holds the code; it says nothing about the body, so the body is
parsed defensively and capped, and a malformed one produces a clear 400 rather
than an exception.

Why the code lives in the URL
-----------------------------

Because the callers are machines configured once, by a person pasting a URL
into another system's settings box. A signature scheme would be better if
TooGather knew which system was calling - GitHub signs with one scheme, Stripe
with another, a shell script with none - and worse in practice here, because
the common case would be a shell script that cannot sign at all. So: one long
random code, revocable by deleting the connector, and the same honesty about
it as an invite link carries.
"""

from __future__ import annotations

import json

from toogather.connectors.base import (
    ConfigField,
    Connector,
    ConnectorError,
    Delivery,
    FetchResult,
    Item,
)

# A single posted note is capped well below the upload limit. A webhook is for
# a summary of something, not for shipping a log file.
MAX_BODY_BYTES = 256 * 1024
MAX_TITLE_CHARS = 200


def _readable_json(payload: object, indent: int = 0) -> str:
    """
    Write arbitrary JSON out as indented text.

    A caller that posts its own structure - a CI result, an issue - should not
    have that turned into a wall of braces. Keys become labels and nesting
    becomes indentation, which is what both a reader and the extractor want.
    """
    pad = "  " * indent
    if isinstance(payload, dict):
        lines = []
        for key, value in payload.items():
            if isinstance(value, dict | list) and value:
                lines.append(f"{pad}{key}:")
                lines.append(_readable_json(value, indent + 1))
            else:
                lines.append(f"{pad}{key}: {_scalar(value)}")
        return "\n".join(lines)
    if isinstance(payload, list):
        return "\n".join(
            _readable_json(entry, indent) if isinstance(entry, dict | list)
            else f"{pad}- {_scalar(entry)}"
            for entry in payload
        )
    return f"{pad}{_scalar(payload)}"


def _scalar(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "yes" if value else "no"
    return str(value)


def parse_delivery(content_type: str, body: bytes, default_title: str) -> Item:
    """
    Turn one posted request into an item.

    Kept separate from the connector so it can be tested without a server, and
    because this is the part that has to be right: it is the only code in
    TooGather that reads a body from an unauthenticated stranger.
    """
    if not body.strip():
        raise ConnectorError("The request had an empty body.")
    if len(body) > MAX_BODY_BYTES:
        raise ConnectorError(
            f"That is larger than the {MAX_BODY_BYTES // 1024} KB a webhook accepts. "
            "Post a summary rather than a full log."
        )

    text = body.decode("utf-8", errors="replace")
    title = default_title

    if "json" in content_type.lower():
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ConnectorError(f"The body said it was JSON but could not be read: {exc}") from exc

        if isinstance(payload, dict) and isinstance(payload.get("content"), str):
            # The documented shape: an explicit title and content.
            content = payload["content"]
            if isinstance(payload.get("title"), str) and payload["title"].strip():
                title = payload["title"]
        else:
            # Anything else: write the whole document out readably.
            content = _readable_json(payload)
            for key in ("title", "subject", "name", "summary"):
                value = payload.get(key) if isinstance(payload, dict) else None
                if isinstance(value, str) and value.strip():
                    title = value
                    break
    else:
        content = text

    if not content.strip():
        raise ConnectorError("There was nothing to record in that request.")
    return Item(external_id="", title=title.strip()[:MAX_TITLE_CHARS] or default_title,
                content=content)


class WebhookConnector(Connector):
    kind = "webhook"
    label = "Webhook"
    inbound = True
    description = (
        "Give something else a URL to post notes to: a CI pipeline, a deploy "
        "script, or another tool's own webhooks. Whatever it posts arrives as "
        "material to review, exactly like an upload."
    )
    fields = (
        ConfigField(
            "default_title", "Default note title",
            placeholder="Posted by CI", required=False,
            help="Used when a caller does not send a title of its own.",
        ),
    )

    def receive(self, delivery: Delivery) -> FetchResult:
        default_title = (str(delivery.config.get("default_title", "")).strip()
                         or "Posted to webhook")
        item = parse_delivery(delivery.content_type, delivery.body, default_title)
        return FetchResult(items=(item,), note=f"Received “{item.title}”.")
