"""Business-layer input-pipeline context."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from bot.service.media_store import WorkspaceScopedMediaStore
from bot.service.pool_router import PoolSessionStore

if TYPE_CHECKING:
    from bot.service.model_choice import ModelChoiceRegistry
from bot.webui.transcript_store import TranscriptStore
from modex_agent.core.session_id import SessionIdFactory
from modex_agent.core.types import InputMessage
from modex_agent.input_pipeline.context import InputContext
from modex_agent.ioc.configs.pool import MediaConfig
from modex_agent.pipeline.adapters import InputAdapter


class BotInputContext(InputContext):
    """Concrete context holding all stage dependencies.

    enqueue_message: S8 calls this to deliver the final InputMessage to the
        channel's physical queue. Per-channel (IM QQ _message_queue / WS queue).
    command_adapter: an InputAdapter whose framework-provided
        _try_intercept_control is reused by the control stages — we only
        relocate the call site into a stage, no framework edits.
    session_factory: SessionIdFactory for creating SessionInfo from external
        conversation ids. Defaults to a fresh factory if not provided.
    media_store: optional workspace+pool-routed inbound byte store. The
        attachment ingest stage persists accepted uploads through it. None
        disables attachment ingest (the stage no-ops when no attachments are
        present, so legacy callers without media wiring still work).
    media_config: perception-gate + budget config (defaults to MediaConfig()).
        Single source of truth for the size caps shared by upload-accept,
        path-injection, and inline-render. Returned by the ``media_config``
        property as the default instance.
    media_config_for_pool: optional resolver ``pool -> MediaConfig`` honoring
        ADR-0013 §7 (per-pool override). When supplied,
        :meth:`media_config_for` delegates to it so each pool's ingest path
        uses that pool's ``PoolConfig.media``. When None (e.g. tests, legacy
        callers), :meth:`media_config_for` falls back to the default
        ``media_config`` instance — existing behavior is preserved.
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
        media_store: WorkspaceScopedMediaStore | None = None,
        media_config: MediaConfig | None = None,
        media_config_for_pool: Callable[[str], MediaConfig] | None = None,
        model_choice_registry: ModelChoiceRegistry | None = None,
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
        self._media_store = media_store
        self._media_config = media_config or MediaConfig()
        self._media_config_for_pool = media_config_for_pool
        self._model_choice_registry = model_choice_registry

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

    @property
    def media_store(self) -> WorkspaceScopedMediaStore | None:
        return self._media_store

    @property
    def media_config(self) -> MediaConfig:
        return self._media_config

    @property
    def model_choice_registry(self) -> ModelChoiceRegistry | None:
        return self._model_choice_registry

    def media_config_for(self, pool: str) -> MediaConfig:
        """Return the perception-gate config to apply for *pool*.

        Honors the ADR-0013 §7 per-pool override: when a ``media_config_for_pool``
        resolver was supplied at construction, it decides the config for the
        given pool; otherwise the default ``media_config`` instance is returned
        so legacy callers and tests without per-pool wiring keep working.
        """
        if self._media_config_for_pool is not None:
            return self._media_config_for_pool(pool)
        return self._media_config
