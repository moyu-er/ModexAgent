"""Telegram IM adapter.

MVP scope:
    - long-polling inbound
    - outbound text + single media (photo/video/voice/document)
    - allow_from allowlist
    - pseudo-streaming via rate-limited message edits

Out of scope (do not implement here):
    - webhooks
    - inline keyboards / reply markups
    - rich layouts
    - reactions
    - reasoning echo
    - media groups (albums)
    - voice transcription

Pure helpers (``telegram_enabled``, ``split_text``, ``markdown_to_html``,
``classify_media``, ``TelegramMediaKind``) live above; the
:class:`TelegramInputAdapter` / :class:`TelegramOutputAdapter` classes below
implement the framework's InputAdapter/OutputAdapter contract. The register
factory (real PTB polling wiring) is appended in a later task.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from enum import Enum
from typing import Any

from modex_agent.core.types import InputMessage, OutputMessage
from modex_agent.input_pipeline.envelope import UserInputEnvelope
from modex_agent.pipeline.adapters import InputAdapter, OutputAdapter

# --- module constants -------------------------------------------------------

_TELEGRAM_TEXT_LIMIT = 4096
_PHOTO_EXT = (".jpg", ".jpeg", ".png", ".webp")
_VIDEO_EXT = (".mp4", ".mov", ".webm", ".m4v")
_VOICE_EXT = (".ogg", ".opus", ".m4a")

# --- precompiled inline-markdown patterns -----------------------------------

_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_CODE_RE = re.compile(r"`(.+?)`")


class TelegramMediaKind(str, Enum):
    """Closed set of outbound media classifications for Telegram."""

    PHOTO = "photo"
    VIDEO = "video"
    VOICE = "voice"
    DOCUMENT = "document"


def telegram_enabled(
    cfg: dict[str, object],
    *,
    enabled: bool | None = None,
) -> bool:
    """Return True iff the Telegram section is enabled AND has a token.

    ``cfg`` is a loose section dict taken from the raw runtime config at
    the boundary where config is still untyped — ``dict[str, object]`` is
    acceptable here as a runtime config boundary.
    """
    is_enabled = enabled if enabled is not None else bool(cfg.get("enabled"))
    return is_enabled and bool(cfg.get("token"))


def split_text(text: str, *, limit: int = _TELEGRAM_TEXT_LIMIT) -> list[str]:
    """Split ``text`` into chunks no longer than ``limit``.

    Prefers newline boundaries: accumulates whole lines (with their
    terminators preserved) and flushes a chunk when the next line would
    exceed the limit. A single line longer than ``limit`` is hard-sliced
    into ``limit``-sized pieces.
    """
    chunks: list[str] = []
    buf = ""
    for line in text.splitlines(keepends=True):
        if len(line) > limit:
            if buf:
                chunks.append(buf)
                buf = ""
            for i in range(0, len(line), limit):
                chunks.append(line[i : i + limit])
            continue
        if buf and len(buf) + len(line) > limit:
            chunks.append(buf)
            buf = ""
        buf += line
    if buf:
        chunks.append(buf)
    return chunks


def markdown_to_html(md: str) -> str:
    """Convert a minimal inline-markdown subset to Telegram HTML.

    Only ``**bold**`` -> ``<b>bold</b>`` and `` `code` `` ->
    ``<code>code</code>`` are handled.
    """
    html = _BOLD_RE.sub(r"<b>\1</b>", md)
    html = _CODE_RE.sub(r"<code>\1</code>", html)
    return html


def classify_media(filename: str) -> TelegramMediaKind:
    """Classify a filename into a :class:`TelegramMediaKind` by extension."""
    name = filename.lower()
    if name.endswith(_PHOTO_EXT):
        return TelegramMediaKind.PHOTO
    if name.endswith(_VIDEO_EXT):
        return TelegramMediaKind.VIDEO
    if name.endswith(_VOICE_EXT):
        return TelegramMediaKind.VOICE
    return TelegramMediaKind.DOCUMENT


_log = logging.getLogger(__name__)


def _chat_id_from_session(session_id: str) -> str:
    """Extract the Telegram chat id from a ``{chat_id}.<agent>...`` session id."""
    return session_id.split(".", 1)[0]


class TelegramInputAdapter(InputAdapter):
    """Telegram inbound adapter.

    Sits on the framework's InputAdapter contract. The PTB ``Application``
    (real long-polling) is injected later by the register factory; without it
    ``start()`` is a safe no-op so the adapter is unit-testable in isolation.
    Inbound text updates are driven through the converged IM input pipeline
    via :meth:`handle_text_message` (mirrors ``QQInputAdapter._on_message``);
    on the continue path the pipeline's S8 stage enqueues the final
    :class:`InputMessage` via :meth:`put_input_message`, which :meth:`receive`
    then drains.
    """

    def __init__(
        self,
        *,
        token: str,
        allow_from: list[str],
        proxy: str | None,
    ) -> None:
        super().__init__()
        self._token: str = token
        self._allow_from: list[str] = allow_from
        self._proxy: str | None = proxy
        self._queue: asyncio.Queue[InputMessage] = asyncio.Queue()
        # PTB start/stop coroutines, injected by the register factory via
        # ``set_lifecycle_hooks``. Keeping them here (rather than holding the
        # PTB Application directly) keeps this adapter PTB-free and unit-
        # testable in isolation — start()/stop() are safe no-ops until wired.
        self._start_hook: Callable[[], Awaitable[None]] | None = None
        self._stop_hook: Callable[[], Awaitable[None]] | None = None

    def set_lifecycle_hooks(
        self,
        *,
        on_start: Callable[[], Awaitable[None]],
        on_stop: Callable[[], Awaitable[None]],
    ) -> None:
        """Inject PTB start/stop coroutines.

        Avoids the factory reaching into private attributes (no SLF001): the
        register factory calls this public seam, then start()/stop() delegate
        to the hooks. The PTB Application itself is captured by the hook
        closures; the adapter never touches PTB attributes directly.
        """
        self._start_hook = on_start
        self._stop_hook = on_stop

    @property
    def name(self) -> str:
        return "telegram"

    def is_allowed(self, sender_id: str) -> bool:
        """True iff *sender_id* passes the allow_from allowlist."""
        return "*" in self._allow_from or sender_id in self._allow_from

    def put_input_message(self, msg: InputMessage) -> None:
        """Push a fully-built InputMessage onto the receive queue.

        The input pipeline's S8 EnqueueStage calls this via
        ``ctx.enqueue_message`` so the stage never touches a Telegram-specific
        method — it just delivers the message and the adapter owns its own
        queue. Mirrors ``QQInputAdapter.put_input_message``.
        """
        self._queue.put_nowait(msg)

    async def handle_text_message(
        self,
        *,
        chat_id: str,
        text: str,
        sender_id: str,
    ) -> None:
        """Drive a Telegram text update through the converged input pipeline.

        Mirrors ``QQInputAdapter._on_message``: allowlist check → build a
        :class:`UserInputEnvelope` seed → run the shared IM pipeline. On a
        ``Terminate`` outcome (pool switch / invalid skill / control notice)
        the response is surfaced directly to this chat; on ``Continue`` the
        pipeline's S8 stage enqueues the final ``InputMessage`` via
        ``ctx.enqueue_message`` (= :meth:`put_input_message`), which
        :meth:`receive` then yields to the agent loop.

        PTB extraction (``effective_message.text`` / ``chat_id`` / user) stays
        in the register factory; this method receives plain strings so it
        remains PTB-free and unit-testable in isolation.
        """
        if not self.is_allowed(sender_id):
            return

        # chat_id is the external conversation id; it seeds the session id
        # (``{chat_id}.<agent>``) built downstream by the session factory, and
        # the output adapter resolves it back via _chat_id_from_session.
        seed = UserInputEnvelope(
            external_id=chat_id,
            content=text,
            channel=self.name,
            explicit_pool=None,
            metadata={
                "chat_id": chat_id,
                "sender_id": sender_id,
            },
        )
        result = await self._input_pipeline.handle(seed, self._input_ctx)

        # Surface Terminate responses (pool switch, invalid skill, etc.)
        if not result.should_continue():
            response = result.response
            msg_text = ""
            if response is not None:
                try:
                    msg_text = str(response.get("message", ""))
                except AttributeError:
                    msg_text = ""
            if msg_text:
                out = self._output_adapter or self._ctrl_output_adapter
                if out is not None:
                    await out.send(OutputMessage(content=msg_text), chat_id)

    async def start(self) -> None:
        """Delegate to the injected PTB start hook, if any.

        No-op until :meth:`set_lifecycle_hooks` wires the real long-polling
        coroutine, so the adapter is unit-testable without any PTB object.
        """
        if self._start_hook is not None:
            await self._start_hook()

    async def stop(self) -> None:
        """Delegate to the injected PTB stop hook, if any."""
        if self._stop_hook is not None:
            await self._stop_hook()

    async def receive(self) -> AsyncIterator[InputMessage]:
        """Yield queued InputMessages indefinitely."""
        while True:
            yield await self._queue.get()


class TelegramOutputAdapter(OutputAdapter):
    """Telegram outbound adapter.

    Renders OutputMessage text to Telegram HTML, chunks it under the 4096-char
    limit, and posts each chunk via the PTB bot's ``send_message``. Media is
    out of MVP scope; text-only keeps the surface minimal.
    """

    def __init__(self, *, bot: object) -> None:
        super().__init__()
        # ``bot`` is a PTB ExtBot — an external-SDK boundary. Stored as Any
        # (the documented escape for external SDK objects the framework does
        # not own) so its ``send_message`` coroutine is callable without a
        # stubbed type. Constructor accepts ``object`` so tests can pass a
        # MagicMock. The destination chat is resolved per-send from the
        # session_id (``{chat_id}.main``), so the adapter fans out to many
        # chats — not a single fixed one.
        self._bot: Any = bot

    @property
    def name(self) -> str:
        return "telegram"

    async def send(self, message: OutputMessage, session_id: str) -> None:
        """Send an OutputMessage's text content to the owning Telegram chat."""
        if message.content:
            await self._send_text(message.content, chat_id=_chat_id_from_session(session_id))

    async def _send_text(self, text: str, *, chat_id: str) -> None:
        """Render *text* to HTML, chunk, and post each chunk.

        On per-chunk send failure (e.g. malformed HTML rejected by Telegram),
        fall back to re-sending the same chunk as plain text. Both attempts
        are swallowed on error so one bad chunk never aborts the whole reply.
        """
        html = markdown_to_html(text)
        send = self._bot.send_message
        for chunk in split_text(html):
            try:
                await send(chat_id=chat_id, text=chunk)
            except Exception:  # noqa: BLE001
                _log.exception("Telegram send_message failed for chunk, retrying as plain text")
                try:
                    await send(chat_id=chat_id, text=chunk)
                except Exception:  # noqa: BLE001
                    _log.exception("Telegram plain-text fallback also failed")

    async def send_delta(
        self,
        delta: str,
        session_id: str,
        metadata: dict[str, object] | None = None,
    ) -> None:
        """Pseudo-streaming: delegate to the base buffering accumulator.

        Rate-limited message editing (real Telegram streaming feel) is a later
        enhancement; MVP buffers like the base class and flushes on turn end.
        """
        await super().send_delta(delta, session_id, metadata)
