"""AgentMaterializer — the subagent construction seam owned by multi_agent.

``AgentTemplate.materialize`` is the subagent entry, but the concrete
assembly body (native component assembly through the plugins assembly
core, external strategy dispatch through the per-agent strategy registry)
composes plugins-layer machinery. ``multi_agent`` sits BELOW ``plugins``
in the layering tree, so the template cannot import that machinery
directly (W5 template/plugins inversion — the last layering-ledger
cluster).

The seam: this ABC declares the materialization contract; the plugins
assembly wiring injects the concrete implementation
(:class:`~modex_agent.plugins.assembly.subagent_materializer.
SubagentMaterializer`) onto :class:`AgentMaterializeDeps.materializer` at
``create_pool`` time. The template computes the delegation snapshot +
sandbox settings (multi_agent-owned concerns), delegates the runtime
construction to the materializer, then applies the shared delegation
boundary — every strategy gets identical post-build wiring.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import TYPE_CHECKING

from modex_agent.core.workspace_root import WorkspaceRootProvider

if TYPE_CHECKING:
    from modex_agent.core.session_id import SessionInfo
    from modex_agent.multi_agent.descriptor import AgentInstance
    from modex_agent.multi_agent.materialize_deps import AgentMaterializeDeps
    from modex_agent.multi_agent.template import AgentTemplate
    from modex_agent.sandbox.delegation import DelegationSnapshot
    from modex_agent.sandbox.settings import SandboxSettings

__all__ = [
    "AgentMaterializer",
    "StaticDelegationRootProvider",
    "subagent_pool_name",
    "subagent_workspace_root",
]


class AgentMaterializer(ABC):
    """Constructs a subagent runtime for :meth:`AgentTemplate.materialize`.

    One method, two roads (mirroring the template's historical dispatch):

    - the EXTERNAL road resolves the subagent's own execution strategy
      from the strategy registry and delegates to
      :meth:`~modex_agent.multi_agent.execution_strategy.ExecutionStrategy.assemble_sub`;
    - the NATIVE road runs the shared native component assembly
      (``assemble_native_agent``) over the compiled per-agent spec.

    The implementation decides the road; the template never branches on
    strategy identity.
    """

    @abstractmethod
    async def materialize(
        self,
        template: AgentTemplate,
        parent_session: SessionInfo | str | None,
        invocation_id: str | None,
        deps: AgentMaterializeDeps,
        *,
        snapshot: DelegationSnapshot,
        settings: SandboxSettings,
    ) -> AgentInstance:
        """Build the subagent AgentInstance for ``template``.

        ``snapshot``/``settings`` are the spawn-time delegation facts the
        template computed (workspace root, resolved sandbox, declared
        depth) — the native road threads them into tool scoping and the
        guard chain; the external road reads the workspace root. The
        caller applies the delegation boundary AFTER this returns.
        """
        ...


class StaticDelegationRootProvider(WorkspaceRootProvider):
    """Frozen workspace-root provider — the delegation snapshot's anchor.

    Both tools and guards use the spawn-time root. Later changes to the
    pool's live provider must not move an already-delegated file boundary.
    Public: shared by the template's delegation wiring and the materializer
    roads (W5 seam extraction).
    """

    def __init__(self, root: Path) -> None:
        self._root = root

    def current(self) -> Path:
        return self._root


def subagent_pool_name(deps: AgentMaterializeDeps) -> str:
    """Read the authoritative pool name from the scope path.

    ``AgentPool`` carries no pool-name attribute; the pool's
    :class:`~modex_agent.workspace.scope_path.ScopePath` is the single
    source of truth (set at pool-wiring time). Falls back to ``"main"``
    when no scope path is wired (non-workspace tests).
    """
    scope_path = deps.scope_path
    if scope_path is not None and scope_path.pool_name:
        return scope_path.pool_name
    return "main"


def subagent_workspace_root(deps: AgentMaterializeDeps) -> Path:
    """Resolve the subagent's workspace root from the threaded authorities.

    ``scope_path.workspace_root`` (the canonical addressing carrier) is
    the primary source; the live ``root_provider`` is the alternative for
    callers that thread one without a scope path. ``project_dir`` is
    deliberately NOT consulted here — it is business-asset lookup only
    (agents/<name>.md prompt templates, data/memory paths), never
    workspace identity: production assemblies pass the static service
    project dir alongside a live workspace handle, and the workspace
    handle must win. All sources absent is a wiring error — raised, not
    silently defaulted.

    Shared by the template entry (snapshot resolution) and both
    materializer roads (rule 15: one mechanism, both callers).
    """
    if deps.scope_path is not None:
        return deps.scope_path.workspace_root
    if deps.root_provider is not None:
        return deps.root_provider.current()
    raise ValueError(
        "AgentTemplate.materialize requires scope_path or root_provider "
        "on AgentMaterializeDeps to resolve the subagent workspace root"
    )
