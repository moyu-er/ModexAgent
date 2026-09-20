"""WebUI streaming event emitters.

Public surface re-exported from the ``emitter`` subpackage: the shared
transcript-recording base (:class:`BotTranscriptEmitter`) plus the WebUI
WebSocket projection (:class:`WebBotEmitter`) and the fan-out
:class:`CompositeEmitter`.
"""

from __future__ import annotations

from .bot_transcript import BotTranscriptEmitter
from .composite import CompositeEmitter
from .web_bot import WebBotEmitter

__all__ = ["BotTranscriptEmitter", "CompositeEmitter", "WebBotEmitter"]
