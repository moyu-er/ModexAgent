"""WebUI streaming event emitters.

Public surface re-exported from the ``emitter`` subpackage so that
``from bot.webui.emitter import WebBotEmitter, CompositeEmitter`` keeps
working after the original ``emitter.py`` module was split into
``web_bot``, ``_segments`` and ``composite``.
"""

from __future__ import annotations

from .composite import CompositeEmitter
from .web_bot import WebBotEmitter

__all__ = ["CompositeEmitter", "WebBotEmitter"]
