"""External coding agent event kinds.

`ExternalEvent` is the closed set of event kinds the provider-event parser
produces from the external CLI's stdout JSONL; the `ExternalAgent` harness
fans them onto the core `TurnEvent` stream through the turn sink. The set
is intentionally small on day one (text / thinking / tool_use / tool_result
/ error) but the parser interface admits additional kinds (status / log /
usage) later without breaking parse call sites.
"""

from __future__ import annotations

from enum import StrEnum


class ExternalEvent(StrEnum):
    """The five day-one event kinds parsed from provider stdout.

    The enum is closed for day-one callers; the parser interface
    (``ProviderEventParser``) emits zero or more of these values per
    stdout JSONL line. New kinds (STATUS, LOG, USAGE) can be appended
    without breaking existing call sites because parsers contract on
    ``Iterator[Emission]`` rather than an exhaustive match.
    """

    TEXT_DELTA = "text_delta"
    THINKING = "thinking"
    TOOL_USE = "tool_use"
    TOOL_RESULT = "tool_result"
    ERROR = "error"


__all__ = ["ExternalEvent"]
