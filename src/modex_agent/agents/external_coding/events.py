"""External coding agent event kinds.

`ExternalCodingEvent` is the closed-set of event kinds emitted through
`ContentEmitter` by the `ExternalCodingAgent` harness. The set is
intentionally small on day one (text / thinking / tool_use / tool_result /
error) but the parser interface admits additional kinds (status / log /
usage) later without breaking emit call sites.
"""

from __future__ import annotations

from enum import StrEnum

from modex_agent.core.events import AgentEvent


class ExternalCodingEvent(AgentEvent, StrEnum):
    """The five day-one event kinds emitted by `ExternalCodingAgent`.

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


__all__ = ["ExternalCodingEvent"]
