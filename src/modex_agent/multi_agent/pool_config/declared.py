"""The declaration-road products one pool consumes at ``create_pool``.

Promoted from ``examples/bot_project/bot/service/pool/declaration.py``
(W4a): the carrier is generic over framework types only (the scope
compilation products + the template registry + the declared pool face),
so the framework pool factory types its ``declared`` input with it. The
boot sequence that PRODUCES it (load → validate → compile → partition)
remains deployment-owned.
"""

from __future__ import annotations

from dataclasses import dataclass

from modex_agent.multi_agent.communication.peer_resolution import PeerLink
from modex_agent.multi_agent.template_registry import AgentTemplateRegistry
from modex_agent.scope.compiler import CompiledAgent
from modex_agent.scope.spec import PoolSpec

__all__ = ["DeclaredPoolBuild"]


@dataclass(frozen=True)
class DeclaredPoolBuild:
    """The declaration-road products one pool consumes at ``create_pool``.

    ``root`` drives the main agent's assembly spec; ``subagents`` seed the
    template registry (lazy materialization reads ``compiled_spec``);
    ``root_children`` are the root's DIRECT children (SPEC §3.2 — the root's
    per-agent communication target store lists them, never grandchildren);
    ``template_registry`` is pre-seeded from the compilation.
    ``pool`` is the declared pool (peers + agent declarations — the single
    pool face create_pool and the strategies read, replacing the legacy
    PoolSpec); ``peer_links`` are this pool's declared links (the env-spec
    agent-pool map reads the peer roots' declared names).
    """

    root: CompiledAgent
    subagents: tuple[CompiledAgent, ...]
    root_children: tuple[CompiledAgent, ...]
    template_registry: AgentTemplateRegistry
    pool: PoolSpec
    peer_links: tuple[PeerLink, ...]
