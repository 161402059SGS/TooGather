"""
The connector interface.

A connector pulls material into a project on a schedule. It does not create
project memory: it writes ordinary `sources` rows, exactly as a person does
when they upload a file, and everything after that - AI proposals, human
review, drift rules - is unchanged. That is the whole design. A connector
that could write confirmed events would be a way to put unreviewed claims
into a team's memory, which is the one thing TooGather is built not to do.

Writing one
-----------

Subclass `Connector`, fill in the four class attributes, implement `fetch`,
and register it:

    from toogather.connectors import register
    from toogather.connectors.base import Connector, FetchResult, Item

    class JiraConnector(Connector):
        kind = "jira"
        label = "Jira"
        description = "Bring in issues closed since the last check."
        fields = (ConfigField("site", "Jira site URL"),
                  ConfigField("token", "API token", secret=True))

        def fetch(self, ctx):
            issues = ...          # ctx.config["site"], ctx.secret, ctx.cursor
            return FetchResult(items=..., cursor=..., note="...")

    register(JiraConnector())

Rules a connector has to follow, because the worker depends on them:

  * `fetch` is called on a worker thread with no request behind it. Raise
    `ConnectorError` with a message a project owner can act on; any other
    exception is logged as a bug and shown as "unexpected error".
  * Honour `ctx.cursor`, and return the new one. The cursor is how the
    connector avoids importing the same material twice, and it is opaque to
    everything except the connector that wrote it.
  * Give every item an `external_id` that is stable for that material. The
    database has a unique index on it, so a repeated run is harmless.
  * Never put a secret in `note`, an item, or an exception message. Those
    are stored and shown on screen.
  * Be bounded. Return at most a few hundred items and let the next run
    continue from the cursor, rather than importing ten years of history in
    one go.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar


class ConnectorError(Exception):
    """
    A connector could not do its job, for a reason a person can act on.

    The message is stored on the connector row and shown to project owners, so
    write it for them: "the repository refused the token" rather than a stack
    trace.
    """


@dataclass(frozen=True)
class ConfigField:
    """One field on the connector's setup form."""

    name: str                       # key in the config dict
    label: str                      # shown above the input
    placeholder: str = ""
    help: str = ""                  # one line under the input
    required: bool = True
    secret: bool = False            # stored encrypted, never shown back


@dataclass(frozen=True)
class Item:
    """One piece of material to bring in. Becomes a `sources` row."""

    external_id: str    # stable id for this material, used to avoid duplicates
    title: str          # what the source is called in the UI
    content: str        # the text itself


@dataclass(frozen=True)
class FetchResult:
    """What one run produced."""

    items: tuple[Item, ...] = ()
    cursor: str = ""     # the new cursor; empty means "keep the one I was given"
    note: str = ""       # one line shown next to the connector: what happened


@dataclass(frozen=True)
class Context:
    """Everything a connector is given for one run."""

    connector_id: str
    project_id: str
    config: dict = field(default_factory=dict)
    secret: str = ""          # the decrypted secret field, or "" if none is set
    cursor: str = ""
    workdir: Path = Path(".")  # a private directory this connector may use
    timeout_seconds: int = 300


class Connector(ABC):
    """Base class for every connector. Instances are stateless and shared."""

    kind: ClassVar[str] = ""
    label: ClassVar[str] = ""
    description: ClassVar[str] = ""
    fields: ClassVar[tuple[ConfigField, ...]] = ()

    def config_problem(self, config: dict) -> str | None:
        """
        Why this configuration cannot be saved, or None if it is fine.

        The default checks that every required non-secret field has a value.
        Override to add real validation - a URL that must be https, an id that
        must be numeric - and call super() first.
        """
        for spec in self.fields:
            if spec.required and not spec.secret and not str(config.get(spec.name, "")).strip():
                return f"{spec.label} is required."
        return None

    @abstractmethod
    def fetch(self, ctx: Context) -> FetchResult:
        """Bring in whatever is new since `ctx.cursor`."""
