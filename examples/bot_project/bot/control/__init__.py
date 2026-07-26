"""Bot control API package (T04).

Wire models (Pydantic) are re-exported here for convenience — they are pure
leaf types with no framework dependencies, so importing them is cheap and
side-effect-free. Server-side components are NOT re-exported here: doing so
would eagerly drag in ``bot.webui.transcript_store`` → ``modex_agent`` (the
full framework) on every submodule import, breaking the ``modexctl`` CLI
which must stay lightweight (only ``pydantic`` + ``httpx``).

Server-side components — import them directly from their submodules:

- :class:`BotControlFacade` / :class:`ControlFacadeError` →
  :mod:`bot.control.facade`
- :func:`project_history_messages` / :func:`project_transcript_history` →
  :mod:`bot.control.history`
- aiohttp route adapter → :mod:`bot.control.routes`

Wire models (re-exported below): :class:`AgentSessionRef`,
:class:`HistoryRequest`, :class:`HistoryResult`, :class:`HistoryMessage`,
:class:`HistorySource`, :class:`SendRequest`, :class:`SendResult`,
:class:`DispatchOutcome`, :class:`ControlError`.
"""

from __future__ import annotations

from bot.control.models import (
    AgentSessionRef,
    ControlError,
    DispatchOutcome,
    HistoryMessage,
    HistoryRequest,
    HistoryResult,
    HistorySource,
    SendRequest,
    SendResult,
)

__all__ = [
    "AgentSessionRef",
    "ControlError",
    "DispatchOutcome",
    "HistoryMessage",
    "HistoryRequest",
    "HistoryResult",
    "HistorySource",
    "SendRequest",
    "SendResult",
]
