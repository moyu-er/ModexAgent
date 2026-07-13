"""External-coding pool wiring helpers for the bot layer.

T10 wiring: resolves ``provider_kind`` from the pool config (the framework's
``MainAgentSpec`` now carries it; raw pool.yml is a fallback), builds the
streaming backend + parser + session store + env spec, and provides a
``DefaultAgentFactory`` subclass that forwards the required keyword-only
collaborators to ``ExternalCodingAgentBuilder.build_agent``.
"""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path
from typing import Any

import yaml

from modex_agent.agents.external_coding.agent import StreamingProviderBackend
from modex_agent.agents.external_coding.builder import ExternalCodingAgentBuilder
from modex_agent.agents.external_coding.contracts import ProviderEventParser
from modex_agent.agents.external_coding.paths import ExternalPaths, ProviderKind
from modex_agent.agents.external_coding.providers.opencode_backend import OpenCodeBackend
from modex_agent.agents.external_coding.providers.opencode_parser import OpenCodeEventParser
from modex_agent.agents.external_coding.providers.pi_backend import PiBackend
from modex_agent.agents.external_coding.providers.pi_parser import PiEventParser
from modex_agent.agents.external_coding.session_store import ExternalSessionStore
from modex_agent.agents.external_coding.types import ExternalEnvSpec
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


# ---------------------------------------------------------------------------
# Provider-kind resolution
# ---------------------------------------------------------------------------


def read_provider_kind(pool_spec: PoolSpec, project_dir: Path) -> ProviderKind:
    """Read ``provider_kind`` from the main spec or raw pool.yml file.

    ``MainAgentSpec`` now carries ``provider_kind`` (added for T10), so the
    preferred source is the parsed spec. If it is missing, fall back to the
    raw YAML for backwards compatibility.
    """
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
    """Return the CLI executable name that ``shutil.which`` should look up."""
    return kind.value


# ---------------------------------------------------------------------------
# Backend / parser factories
# ---------------------------------------------------------------------------


def build_external_coding_backend(kind: ProviderKind) -> StreamingProviderBackend:
    """Build a streaming backend for the configured provider kind.

    The model is supplied per-turn by ``ExternalCodingAgent`` via
    ``ExecOptions.model``, so the backend constructor only needs the
    provider family.
    """
    if kind == ProviderKind.OPENCODE:
        return OpenCodeBackend()
    return PiBackend(provider=None)


def build_external_coding_parser(kind: ProviderKind) -> ProviderEventParser:
    """Build a parser matching the provider's stdout JSONL shape."""
    if kind == ProviderKind.OPENCODE:
        return OpenCodeEventParser()
    return PiEventParser()


# ---------------------------------------------------------------------------
# Env spec construction
# ---------------------------------------------------------------------------


def _modexctl_bin_dir() -> Path:
    exe = shutil.which("modexctl")
    if exe:
        return Path(exe).parent
    logger.warning("modexctl not found on PATH; falling back to '.' for modexctl_bin_dir")
    return Path(".")


def _build_agent_pool_map(
    pool_name: str, pool_spec: PoolSpec, project_dir: Path
) -> dict[str, str]:
    """Map every reachable agent name (main/subagents/peers) to its pool."""
    pool_map: dict[str, str] = {pool_spec.main.agent_name: pool_name}
    for sub in pool_spec.subagents:
        pool_map[sub.agent_name] = pool_name
    store = PoolStore(base_dir=project_dir)
    for peer in pool_spec.peers:
        try:
            peer_spec = store.read_pool(peer)
        except Exception as exc:  # noqa: BLE001 -- best-effort peer lookup
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
    """Build the (name, description) target list from subagents and peers."""
    targets: list[tuple[str, str]] = []
    for sub in pool_spec.subagents:
        targets.append((sub.agent_name, sub.description or f"{sub.agent_name} subagent"))
    store = PoolStore(base_dir=project_dir)
    for peer in pool_spec.peers:
        try:
            peer_spec = store.read_pool(peer)
        except Exception as exc:  # noqa: BLE001 -- best-effort peer lookup
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
    """Construct the ``ExternalEnvSpec`` template.

    ``session_id`` is a placeholder here — ``ExternalCodingAgent._run_turn``
    overwrites it with the real per-turn ``ctx.session.session_id`` before
    building the spawn env.  Similarly ``provider_session_id`` is resolved
    per-turn from :class:`ExternalSessionStore`.
    """
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


# ---------------------------------------------------------------------------
# Collated deps
# ---------------------------------------------------------------------------


def build_external_coding_deps(
    pool_name: str,
    pool_spec: PoolSpec,
    project_dir: Path,
    inbox_dir: Path,
    workspace_dir: Path,
    main_agent_name: str,
    base_env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Build the keyword-only collaborator dict for ``ExternalCodingAgentBuilder``.

    The returned dict is suitable for passing to ``ExternalCodingAwareFactory``.
    """
    provider_kind = read_provider_kind(pool_spec, project_dir)
    backend = build_external_coding_backend(provider_kind)
    parser = build_external_coding_parser(provider_kind)
    session_store = ExternalSessionStore(ExternalPaths(workspace_dir))
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


# ---------------------------------------------------------------------------
# Factory subclass
# ---------------------------------------------------------------------------


class ExternalCodingAwareFactory(DefaultAgentFactory):
    """DefaultAgentFactory that can build external_coding main agents.

    For ``react`` / ``pipeline`` descriptors the behavior is identical to the
    parent class. For ``external_coding`` descriptors it forwards the
    pre-built runtime collaborators (backend, parser, session store, env spec,
    provider kind, base env) to ``ExternalCodingAgentBuilder.build_agent``.
    """

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
