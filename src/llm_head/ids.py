"""Request IDs in Olla's format: <adjective>-<action>-<4 hex>, e.g. "gentle-humming-597a"."""

from __future__ import annotations

import secrets

# Word lists from Olla v0.0.28 (internal/util/request.go).
_ADJ = ["huacaya", "suri", "vicuna", "alpaca", "guanaco", "woolly", "silky", "fluffy", "curly", "shaggy",
        "noble", "gentle", "swift", "steady", "proud"]
_ACTION = ["grazing", "trekking", "humming", "spitting", "prancing", "carrying", "leading", "following",
           "resting", "alerting", "browsing", "foraging", "wandering", "galloping", "ambling"]


def new_request_id() -> str:
    return f"{secrets.choice(_ADJ)}-{secrets.choice(_ACTION)}-{secrets.randbelow(0x10000):04x}"


def accept_request_id(value: str | None) -> str | None:
    """Reuse a client's X-Request-ID if it is 1-128 printable ASCII characters, as Olla does."""
    if value and len(value) <= 128 and all(0x21 <= ord(c) <= 0x7E for c in value):
        return value
    return None
