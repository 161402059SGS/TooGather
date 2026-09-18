"""
The connector registry.

Connectors are looked up by `kind`, which is the string stored on the
connector row. Keeping the lookup in one small module means the web app and
the worker agree on what exists, and that a connector nobody installed simply
does not appear rather than breaking a page.

A connector whose class has been removed leaves rows behind that name a kind
nobody serves any more. `get()` returns None for those, and both the worker
and the settings page treat that as "this connector is not available on this
server" rather than crashing.
"""

from __future__ import annotations

from toogather.connectors.base import (
    ConfigField,
    Connector,
    ConnectorError,
    Context,
    Delivery,
    FetchResult,
    Item,
)

__all__ = [
    "ConfigField", "Connector", "ConnectorError", "Context", "Delivery",
    "FetchResult", "Item", "register", "get", "available", "pollable",
]

_REGISTRY: dict[str, Connector] = {}


def register(connector: Connector) -> Connector:
    """Make a connector available. Called once per class, at import time."""
    if not connector.kind:
        raise ValueError("A connector needs a kind.")
    _REGISTRY[connector.kind] = connector
    return connector


def get(kind: str) -> Connector | None:
    """The connector for this kind, or None if this server does not have it."""
    return _REGISTRY.get(kind)


def available() -> list[Connector]:
    """Every registered connector, in label order, for the setup form."""
    return sorted(_REGISTRY.values(), key=lambda c: c.label)


def pollable(kind: str) -> bool:
    """
    Should a connector of this kind be put on the worker's schedule?

    False for an inbound connector, and false for a kind this server does not
    have - the caller is deciding what to store, and a connector nobody serves
    should not sit in the due queue waiting to fail.
    """
    connector = get(kind)
    return connector is not None and not connector.inbound


# Built-in connectors are registered here rather than registering themselves,
# so a connector module imports only `base` and this package stays the single
# place that decides what ships. The import sits at the bottom because
# registration needs `register` to exist first.
from toogather.connectors.email import EmailConnector  # noqa: E402
from toogather.connectors.git import GitConnector  # noqa: E402
from toogather.connectors.webhook import WebhookConnector  # noqa: E402

register(GitConnector())
register(EmailConnector())
register(WebhookConnector())
