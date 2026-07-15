"""External-coding pool wiring helpers for the bot layer.

T10 wiring: resolves ``provider_kind`` from the pool config (the framework's
``MainAgentSpec`` now carries it; raw pool.yml is a fallback), builds the
streaming backend + session store + env spec, and provides a
``DefaultAgentFactory`` subclass that forwards the required keyword-only
collaborators to ``ExternalCodingAgentBuilder.build_agent``.

OpenCode backend selection is hardcoded: SSE (``opencode serve``) is the
default transport; subprocess (``opencode run``) is the runtime fallback
when the SSE server fails to start. This is NOT configurable via pool.yml.
"""

from __future__ import annotations

import logging
import os
import shutil
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import yaml

from modex_agent.agents.external_coding.agent import StreamingProviderBackend
from modex_agent.agents.external_coding.builder import ExternalCodingAgentBuilder
from modex_agent.agents.external_coding.contracts import ProviderEventParser
from modex_agent.agents.external_coding.paths import ProviderKind
from modex_agent.agents.external_coding.providers.opencode_backend import OpenCodeBackend
from modex_agent.agents.external_coding.providers.opencode_server_backend import (
    OpenCodeServerBackend,
    SSEUnavailableError,
)
from modex_agent.agents.external_coding.providers.opencode_sse_parser import OpenCodeSSEParser
from modex_agent.agents.external_coding.providers.pi_backend import PiBackend
from modex_agent.agents.external_coding.providers.pi_parser import PiEventParser
from modex_agent.agents.external_coding.types import (
    BackendResult,
    Emission,
    ExecOptions,
    ExternalEnvSpec,
)
from modex_agent.core.agent import Agent
from modex_agent.core.constants import ExecutionStrategy
from modex_agent.multi_agent.descriptor import AgentDescriptor
from modex_agent.multi_agent.factory import DefaultAgentFactory
from modex_agent.multi_agent.pool_config import PoolStore
from modex_agent.multi_agent.pool_config.specs import PoolSpec

logger = logging.getLogger(__name__)

__all__ = [
    "ExternalCodingAwareFactory",
    "build_external_coding_backend",
    "build_external_coding_deps",
    "build_external_coding_env_spec",
    "build_external_coding_parser",
    "provider_executable_for",
    "read_provider_kind",
]


def read_provider_kind(pool_spec: PoolSpec, project_dir: Path) -> ProviderKind:
    if pool_spec.main.provider_kind is not None:
        return pool_spec.main.provider_kind
    pool_yml = project_dir / "config" / "pools" / pool_spec.name / "pool.yml"
    if not pool_yml.exists():
        return ProviderKind.PI
    data: Any = yaml.safe_load(pool_yml.read_text(encoding="utf-8")) or {}
    kind = data.get("provider_kind", "pi")
    if not isinstance(kind, str):
        return ProviderKind.PI
    return _provider_kind_from_str(kind)


def _provider_kind_from_str(value: str) -> ProviderKind:
    if value == ProviderKind.OPENCODE.value:
        return ProviderKind.OPENCODE
    return ProviderKind.PI


def provider_executable_for(kind: ProviderKind) -> str:
    return kind.value


def build_external_coding_backend(kind: ProviderKind) -> StreamingProviderBackend:
    if kind == ProviderKind.OPENCODE:
        return _OpenCodeFallbackBackend()
    return PiBackend(provider=None)


def build_external_coding_parser(kind: ProviderKind) -> ProviderEventParser:
    if kind == ProviderKind.OPENCODE:
        return OpenCodeSSEParser()
    return PiEventParser()


class _OpenCodeFallbackBackend(StreamingProviderBackend):
    """SSE-first backend with automatic subprocess fallback.

    OpenCode SSE (``opencode serve``) is the hardcoded default. If the
    SSE server fails to start, each turn falls back to subprocess
    (``opencode run``). The fallback is sticky — once SSE fails, all
    subsequent turns use subprocess to avoid repeated startup failures.

    TODO: OpenCode subagent (task tool) child-session tracking is not
    yet implemented. The SSE event stream carries child session events
    (session.created with parentID, child message.part.delta/updated),
    but the backend currently filters them out at the parser level.
    Implementing child-session support requires: (1) per-session parser
    demuxing in the backend, (2) correlating session.created with task
    tool part subagent_type, (3) registering child sessions in
    SessionRegistry with correct parent_session_id.
    """

    def __init__(self) -> None:
        self._sse_backend = OpenCodeServerBackend()
        self._subprocess_backend = OpenCodeBackend()
        self._fallback_active = False

    async def execute_streaming(
        self,
        opts: ExecOptions,
        env: dict[str, str],
        on_emission: Callable[[Emission], Awaitable[None]],
    ) -> BackendResult:
        if not self._fallback_active:
            try:
                return await self._sse_backend.execute_streaming(
                    opts, env, on_emission
                )
            except SSEUnavailableError as exc:
                logger.warning(
                    "OpenCode SSE backend unavailable, falling back to subprocess: %s",
                    exc,
                )
                self._fallback_active = True
        return await self._subprocess_backend.execute_streaming(
            opts, env, on_emission
        )

    async def close(self) -> None:
        first_error: BaseException | None = None
        try:
            await self._sse_backend.close()
        except BaseException as exc:
            first_error = exc
        try:
            await self._subprocess_backend.close()
        except BaseException as exc:
            if first_error is None:
                first_error = exc
        if first_error is not None:
            raise first_error


def _modexctl_bin_dir() -> Path:
    exe = shutil.which("modexctl")
    if exe:
        return Path(exe).parent
    logger.warning("modexctl not found on PATH; falling back to '.' for modexctl_bin_dir")
    return Path(".")


def _build_agent_pool_map(
    pool_name: str, pool_spec: PoolSpec, project_dir: Path
) -> dict[str, str]:
    pool_map: dict[str, str] = {pool_spec.main.agent_name: pool_name}
    for sub in pool_spec.subagents:
        pool_map[sub.agent_name] = pool_name
    store = PoolStore(base_dir=project_dir)
    for peer in pool_spec.peers:
        try:
            peer_spec = store.read_pool(peer)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Pool '%s': cannot read peer pool %r for agent_pool_map: %s",
                pool_name, peer, exc,
            )
            continue
        pool_map[peer_spec.main.agent_name] = peer
    return pool_map


def _build_targets(
    pool_name: str, pool_spec: PoolSpec, project_dir: Path
) -> list[tuple[str, str]]:
    targets: list[tuple[str, str]] = []
    for sub in pool_spec.subagents:
        targets.append((sub.agent_name, sub.description or f"{sub.agent_name} subagent"))
    store = PoolStore(base_dir=project_dir)
    for peer in pool_spec.peers:
        try:
            peer_spec = store.read_pool(peer)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Pool '%s': cannot read peer pool %r for targets: %s",
                pool_name, peer, exc,
            )
            continue
        desc = peer_spec.main.description or f"Peer pool {peer}'s main agent"
        targets.append((peer_spec.main.agent_name, desc))
    return targets


def build_external_coding_env_spec(
    pool_name: str,
    pool_spec: PoolSpec,
    project_dir: Path,
    inbox_dir: Path,
    workspace_dir: Path,
    main_agent_name: str,
) -> ExternalEnvSpec:
    return ExternalEnvSpec(
        workspace_root=workspace_dir,
        inbox_root=inbox_dir.parent,
        workdir=workspace_dir,
        session_id=f"__pending__.{main_agent_name}",
        agent_name=main_agent_name,
        provider_session_id="",
        agent_pool_map=_build_agent_pool_map(pool_name, pool_spec, project_dir),
        targets=_build_targets(pool_name, pool_spec, project_dir),
        modexctl_bin_dir=_modexctl_bin_dir(),
    )


def build_external_coding_deps(
    pool_name: str,
    pool_spec: PoolSpec,
    project_dir: Path,
    inbox_dir: Path,
    workspace_dir: Path,
    main_agent_name: str,
    base_env: dict[str, str] | None = None,
    *,
    app_config: Any | None = None,
    persistence: Any | None = None,
) -> dict[str, Any]:
    provider_kind = read_provider_kind(pool_spec, project_dir)
    backend = build_external_coding_backend(provider_kind)
    parser = build_external_coding_parser(provider_kind)
    from bot.service.builders import build_external_session_map_store
    from modex_agent.core.scope import RecordScope

    session_store = build_external_session_map_store(
        app_config,
        persistence,
        workspace_dir,
        RecordScope(pool=pool_name),
    )
    spec = build_external_coding_env_spec(
        pool_name, pool_spec, project_dir, inbox_dir, workspace_dir, main_agent_name
    )
    return {
        "backend": backend,
        "session_store": session_store,
        "parser": parser,
        "provider_kind": provider_kind,
        "spec": spec,
        "base_env": dict(base_env) if base_env is not None else dict(os.environ),
    }


class ExternalCodingAwareFactory(DefaultAgentFactory):
    def __init__(
        self,
        *args: Any,  # noqa: ANN401
        external_coding_deps: dict[str, Any] | None = None,  # noqa: ANN401
        **kwargs: Any,  # noqa: ANN401
    ) -> None:
        super().__init__(*args, **kwargs)
        self._external_coding_deps: dict[str, Any] = external_coding_deps or {}  # noqa: ANN401

    def _build_agent(self, descriptor: AgentDescriptor, provider: Any) -> Agent[Any]:  # noqa: ANN401
        if descriptor.execution_strategy == ExecutionStrategy.EXTERNAL_CODING:
            deps = self._external_coding_deps
            missing = [
                name
                for name in ("backend", "session_store", "parser", "provider_kind", "spec")
                if deps.get(name) is None
            ]
            if missing:
                raise ValueError(
                    f"ExternalCodingAwareFactory missing external_coding deps: {', '.join(missing)}"
                )
            return ExternalCodingAgentBuilder.build_agent(
                descriptor,
                provider,
                backend=deps["backend"],
                session_store=deps["session_store"],
                parser=deps["parser"],
                provider_kind=deps["provider_kind"],
                spec=deps["spec"],
                base_env=deps.get("base_env"),
            )
        return super()._build_agent(descriptor, provider)
