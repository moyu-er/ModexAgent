"""One editor-owned process binds one project and reuses the bot resource lifecycle."""
from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml
from pydantic import BaseModel, ConfigDict

from bot.service.core import BotService
from modex_agent.acp.backend import AcpSessionBackend, AcpSessionHandle
from modex_agent.acp.types import AcpBackendError, AcpBackendErrorCode, AcpOpenKind, AcpOpenRequest
from modex_agent.core.agent import ExecutionStrategyKind
from modex_agent.core.emitter import ContentEmitter
from modex_agent.core.message import ChatMessage
from modex_agent.core.session_id import SessionInfo
from modex_agent.messaging.models import InputMessage
from modex_agent.multi_agent.pool import AgentPool
from modex_agent.pipeline.adapters import InputAdapter

from .identity import create_acp_session, validate_acp_session

if TYPE_CHECKING:
    from bot.input_pipeline.context import BotInputContext
    from bot.input_pipeline.prepare import BotInputPreparation
    from bot.webui.transcript_store import TranscriptStore
    from bot.workspace.handle import PoolWorkspaceResources
    from modex_agent.multi_agent.pool_instance import PoolInstance

    from .emitter import AcpEmitterHub, AcpOutputAdapter

logger = logging.getLogger(__name__)


class AcpEntryConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    pool: str | None = None


def _load_entry_config(config_dir: Path, pool_override: str | None) -> AcpEntryConfig:
    raw = yaml.safe_load((config_dir / "bot_config.yml").read_text(encoding="utf-8")) or {}
    config = AcpEntryConfig.model_validate(raw.get("acp") or {})
    return config if pool_override is None else AcpEntryConfig(pool=pool_override)


class _NullInputAdapter(InputAdapter):
    @property
    def name(self) -> str:
        return "acp"

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def receive(self) -> AsyncIterator[InputMessage]:
        return
        yield


class AcpRuntime(AcpSessionBackend):
    hub: AcpEmitterHub
    output: AcpOutputAdapter
    preparation: BotInputPreparation
    input_context: BotInputContext

    def __init__(self, config_dir: Path, config: AcpEntryConfig) -> None:
        self._config_dir = config_dir.resolve()
        self._config = config
        self._service: BotService | None = None
        self._resources: PoolWorkspaceResources | None = None
        self._instance: PoolInstance | None = None
        self._project_root: Path | None = None
        self._boot_task: asyncio.Task[None] | None = None
        self._close_task: asyncio.Task[None] | None = None
        self._closed = False
        self._handles: dict[str, AcpSessionHandle] = {}
        self._opening_sessions: set[str] = set()
        self._transcript: TranscriptStore | None = None
        self._lifecycle_stack: contextlib.AsyncExitStack | None = None

    @property
    def pool(self) -> AgentPool:
        if self._instance is None:
            raise RuntimeError("Project pool is not ready")
        return self._instance.pool

    @property
    def supports_load(self) -> bool:
        return True

    @property
    def project_root(self) -> Path | None:
        return self._project_root

    async def bind_project(self, cwd: Path) -> None:
        if self._closed:
            raise RuntimeError("ACP runtime is closed")
        if not cwd.is_absolute():
            raise ValueError("ACP cwd must be an absolute directory")
        root = cwd.resolve()
        if not root.is_dir():
            raise ValueError("ACP cwd must be an existing directory")
        if self._project_root is not None and self._project_root != root:
            raise ValueError("ACP instance is already bound to a different project")
        if self._boot_task is None:
            self._project_root = root
            self._boot_task = asyncio.create_task(self._boot(root))
        try:
            await asyncio.shield(self._boot_task)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Boot has already finished with the failure, so close() cannot
            # self-wait on this caller. Drain the partially started service
            # through the single close owner (also flips _closed, preventing
            # rebinding) instead of leaking live resources to process exit.
            await self.close()
            raise

    async def open(self, request: AcpOpenRequest) -> AcpSessionHandle:
        if self._closed:
            raise AcpBackendError(AcpBackendErrorCode.BOOT_FAILED, "ACP runtime is closed")
        if not request.cwd.is_absolute() or not request.cwd.is_dir():
            raise AcpBackendError(AcpBackendErrorCode.INVALID_CWD, "cwd must be an absolute existing directory")
        if self._project_root is not None and self._project_root != request.cwd.resolve():
            raise AcpBackendError(AcpBackendErrorCode.PROJECT_MISMATCH, "Instance is bound to a different project")
        try:
            await self.bind_project(request.cwd)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise AcpBackendError(AcpBackendErrorCode.BOOT_FAILED, str(exc)) from exc
        if self._closed:
            raise AcpBackendError(AcpBackendErrorCode.BOOT_FAILED, "ACP runtime is closed")
        if request.session_id is None:
            return await self._open_bound_session(request)
        session_id = request.session_id
        if session_id in self._handles or session_id in self._opening_sessions:
            raise AcpBackendError(AcpBackendErrorCode.BUSY, "Session is already open or loading")
        self._opening_sessions.add(session_id)
        try:
            return await self._open_bound_session(request)
        finally:
            self._opening_sessions.discard(session_id)

    async def _open_bound_session(self, request: AcpOpenRequest) -> AcpSessionHandle:
        from .driver import PoolAcpSessionHandle

        instance = self._instance
        resources = self._resources
        if instance is None or resources is None:
            raise AcpBackendError(AcpBackendErrorCode.BOOT_FAILED, "Project resources are not ready")
        pool = instance.pool
        registry = pool.session_registry
        tree = pool.tree
        if registry is None or tree is None:
            raise AcpBackendError(AcpBackendErrorCode.BOOT_FAILED, "Pool session lifecycle is not assembled")
        if request.kind == AcpOpenKind.NEW:
            session = create_acp_session(pool_name=instance.name, agent_name=instance.root_agent_name)
            await registry.register(session)
            await tree.register_request_scoped(session.session_id)
        else:
            assert request.session_id is not None
            if request.session_id in self._handles:
                raise AcpBackendError(AcpBackendErrorCode.BUSY, "Session is already open")
            session = await resources.session_index_store.get(request.session_id)
            if session is None:
                raise AcpBackendError(AcpBackendErrorCode.NOT_FOUND, "Unknown session")
            try:
                validate_acp_session(session, pool_name=instance.name, agent_name=instance.root_agent_name)
            except ValueError as exc:
                raise AcpBackendError(AcpBackendErrorCode.UNSUPPORTED, str(exc)) from exc
            if not await tree.is_request_scoped(session.session_id):
                raise AcpBackendError(AcpBackendErrorCode.UNSUPPORTED, "Session request policy is incomplete")
        if self._closed:
            raise AcpBackendError(AcpBackendErrorCode.BOOT_FAILED, "ACP runtime is closed")
        handle = PoolAcpSessionHandle(self, session)
        self._handles[session.session_id] = handle
        return handle

    async def _boot(self, project_root: Path) -> None:
        from dotenv import load_dotenv

        from bot.service.roots import BotAssemblyRoots

        from .emitter import AcpEmitterHub, AcpOutputAdapter, AcpTurnEmitter

        load_dotenv(self._config_dir.parent / ".env")
        hub = AcpEmitterHub(resolver=self._root_session_for)
        output = AcpOutputAdapter(hub)
        resource_root = Path(__file__).resolve().parents[2]

        # The ACP entry never runs BotService.start() (it blocks on the
        # shutdown event), so the shared ``opencode serve`` singleton's
        # process-lifecycle owner is bound here through the SAME
        # ``OpenCodeServerManager.lifecycle()`` context the resident entry
        # enters in BotService.start — no parallel manager, no provider
        # branch. Entering does NOT spawn the process: it stays lazy on the
        # first external-agent acquire(). Held on one async ExitStack so
        # _close() releases it in LIFO order AFTER service.stop().
        from modex_agent.agents.external.providers.opencode.server_manager import (
            OpenCodeServerManager,
        )

        self._lifecycle_stack = contextlib.AsyncExitStack()
        await self._lifecycle_stack.enter_async_context(OpenCodeServerManager.lifecycle())

        def emitter_factory(session_id: str, pool: str) -> ContentEmitter[Any]:
            return AcpTurnEmitter(
                hub,
                session_id,
                pool=pool,
                transcript_store=self._transcript,
            )

        service = BotService(
            self._config_dir,
            _NullInputAdapter(),
            output,
            emitter_factory,
            roots=BotAssemblyRoots(
                config_dir=self._config_dir,
                resource_root=resource_root,
                workspace_home=project_root,
            ),
            enable_dynamic_workspaces=False,
        )
        self._service = service
        await service.initialize()
        self._resources = service.home_resources
        assert self._resources is not None, "home resources must materialize during initialize"
        self._instance = self._select_pool(self._resources.pools)
        if self._instance.main_execution_strategy != ExecutionStrategyKind.REACT:
            raise ValueError("ACP currently requires a native ReAct main agent")
        self.hub = hub
        self.output = output
        await self._prepare_inputs(service)
        tree = self.pool.tree
        if tree is None:
            raise RuntimeError("Pool session tree is not assembled")
        await tree.settle_interrupted_scopes()

    async def _root_session_for(self, session_id: str) -> str | None:
        registry = self.pool.session_registry
        if registry is None:
            return None
        visited: set[str] = set()
        current: str | None = session_id
        while current is not None and current not in visited:
            if current in self._handles:
                return current
            visited.add(current)
            info = await registry.get(current)
            current = info.parent_session_id if info is not None else None
        return None

    async def _prepare_inputs(self, service: BotService) -> None:
        from bot.input_pipeline.assembly import build_acp_pipeline
        from bot.input_pipeline.context import BotInputContext
        from bot.input_pipeline.stages.skill_parse import PoolSkillResolverRegistry
        from bot.persistence.transcript import build_transcript_store_resolver
        from bot.service.workspace_store import WorkspaceScopedTranscriptStore
        from bot.webui.workspace_providers import workspace_transcript_store_for_sessions

        resources = self._resources
        instance = self._instance
        assert resources is not None and instance is not None
        # Transcript persistence mirrors WebUIService (web_ui_service.py): one
        # workspace-scoped store. FILE routes to the scoped store's built-in
        # pool-partitioned file adapter; SQLITE resolves the workspace's
        # ResilientTranscriptStore through the shared helper. A concrete store
        # here would reject the emitter's sessions_dir argument (TypeError).
        app_config = service._app_config
        assert app_config is not None, "AppConfig must be loaded before ACP input preparation"
        transcript = WorkspaceScopedTranscriptStore(
            data_dir_name=app_config.paths.data_dir_name,
            store_resolver=build_transcript_store_resolver(
                app_config.persistence.backend,
                lambda sessions_dir: workspace_transcript_store_for_sessions(
                    service.workspace_stack, sessions_dir
                ),
            ),
        )
        self._transcript = transcript
        # Owner-visible wiring (same as WebUIService): the service-level
        # store slot and the materialized home bundle both carry the scoped
        # store so the EXISTING owner path releases it — _stop_resources →
        # release_workspace on workspace eviction (wiring/resources.py).
        service._transcript_store = transcript
        if self._resources is not None:
            self._resources.transcript_store = transcript
        assembly = service.assembly_context
        routing = service.pool_session_store
        if assembly is None or routing is None or resources.component_registry is None:
            raise RuntimeError("Bot input dependencies are not assembled")
        skills = PoolSkillResolverRegistry(lambda workspace, pool: instance.skill_resolver if workspace == resources.target and pool == instance.name else None)
        self.preparation = await build_acp_pipeline(
            registry=resources.component_registry, ctx=assembly, skill_registry=skills,
        )
        self.input_context = BotInputContext(
            default_pool=instance.name,
            pool_session_store=routing,
            agent_resolver=lambda pool: instance.root_agent_name,
            transcript_store=transcript,
            enqueue_message=self._reject_enqueue,
            command_adapter=service.input_adapter,
            current_ws_provider=lambda: resources.target,
            available_pools=lambda: {instance.name},
        )

    def release_handle(self, handle: AcpSessionHandle) -> None:
        if self._handles.get(handle.session_id) is handle:
            self._handles.pop(handle.session_id)

    @staticmethod
    def _reject_enqueue(message: InputMessage) -> None:
        raise RuntimeError("Editor inputs must use pool request admission")

    def _select_pool(self, pools: Mapping[str, PoolInstance]) -> PoolInstance:
        if self._config.pool is not None:
            instance = pools.get(self._config.pool)
            if instance is None:
                raise ValueError(f"Configured ACP pool {self._config.pool!r} is unavailable")
            return instance
        if len(pools) == 1:
            return next(iter(pools.values()))
        if "default" in pools:
            return pools["default"]
        raise ValueError("Set acp.pool or --pool when no unambiguous default pool exists")

    async def read_history(self, session: SessionInfo) -> list[ChatMessage]:
        from bot.control.history import read_pool_session_history

        if self._resources is None or self._instance is None:
            raise RuntimeError("Project resources are not ready")
        return await read_pool_session_history(
            self._resources, pool=self._instance.name, session_id=session.session_id,
        )

    async def close(self) -> None:
        if self._close_task is None:
            self._closed = True
            self._close_task = asyncio.create_task(self._close())
        await asyncio.shield(self._close_task)

    async def _close(self) -> None:
        boot = self._boot_task
        if boot is not None:
            if not boot.done():
                boot.cancel()
            try:
                await boot
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.debug("ACP bootstrap failed before shutdown", exc_info=True)
        errors: list[Exception] = []
        for handle in tuple(self._handles.values()):
            try:
                await handle.close()
            except Exception as exc:
                errors.append(exc)
        self._handles.clear()
        if self._service is not None:
            try:
                await self._service.stop()
            except Exception as exc:
                errors.append(exc)
        # Release the shared ``opencode serve`` lifecycle LAST (ExitStack
        # LIFO): service.stop() already evicted workspaces and drained the
        # pools, so no external turn can still hold the shared process.
        stack = self._lifecycle_stack
        self._lifecycle_stack = None
        if stack is not None:
            try:
                await stack.aclose()
            except Exception as exc:
                errors.append(exc)
        if errors:
            raise ExceptionGroup("ACP shutdown incomplete", errors)


def run_acp_entry(config_dir: Path, *, pool_override: str | None = None) -> None:
    from modex_agent.acp.entry import main

    main(AcpRuntime(config_dir, _load_entry_config(config_dir, pool_override)))
