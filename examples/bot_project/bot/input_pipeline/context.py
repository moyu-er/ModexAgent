"""Business-layer input-pipeline context."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from bot.service.pool_router import PoolSessionStore
from bot.webui.transcript_store import TranscriptStore
from framework.core.session_id import SessionIdFactory
from framework.core.types import InputMessage
from framework.input_pipeline.context import InputContext
from framework.pipeline.adapters import InputAdapter


class BotInputContext(InputContext):
    """Concrete context holding all stage dependencies.

    enqueue_message: S8 calls this to deliver the final InputMessage to the
        channel's physical queue. Per-channel (IM QQ _message_queue / WS queue).
    command_adapter: an InputAdapter whose framework-provided
        _try_intercept_control is reused by the control stages — we only
        relocate the call site into a stage, no framework edits.
    session_factory: SessionIdFactory for creating SessionInfo from external
        conversation ids. Defaults to a fresh factory if not provided.
    """

    def __init__(
        self,
        *,
        default_pool: str,
        pool_session_store: PoolSessionStore,
        agent_pool_map: dict[str, str],
        agent_resolver: Callable[[str], str],
        transcript_store: TranscriptStore,
        enqueue_message: Callable[[InputMessage], None],
        command_adapter: InputAdapter,
        session_factory: SessionIdFactory | None = None,
        current_ws_provider: Callable[[], Path] | None = None,
    ) -> None:
        self._default_pool = default_pool
        self._pool_session_store = pool_session_store
        self._agent_pool_map = dict(agent_pool_map)
        self._agent_resolver = agent_resolver
        self._transcript_store = transcript_store
        self._enqueue_message = enqueue_message
        self._command_adapter = command_adapter
        self._session_factory = session_factory or SessionIdFactory()
        self._current_ws_provider = current_ws_provider or (lambda: Path.cwd())

    def current_ws(self) -> Path:
        return self._current_ws_provider()

    @property
    def default_pool(self) -> str:
        return self._default_pool

    def pool_for_agent(self, agent: str) -> str:
        return self._agent_pool_map.get(agent, self._default_pool)

    def agent_for_pool(self, pool: str) -> str:
        return self._agent_resolver(pool)

    # accessors used by stages
    @property
    def pool_session_store(self) -> PoolSessionStore:
        return self._pool_session_store

    @property
    def transcript_store(self) -> TranscriptStore:
        return self._transcript_store

    def enqueue_message(self, msg: InputMessage) -> None:
        self._enqueue_message(msg)

    @property
    def command_adapter(self) -> InputAdapter:
        return self._command_adapter

    @property
    def session_factory(self) -> SessionIdFactory:
        return self._session_factory
