"""The N2 hot-reload seam: spec-hash + per-pool generation counter
(SPEC §10, ticket 18).

Zero-cost reservation — nothing in the runtime consumes this module yet.
Per SPEC §10's hot-reload row (热生效 N2) and the N2 decision
(restart-effective stands), the future swap mechanism — old agents
finishing in-flight turns (PipelineSnapshot-style pinning) plus
approval-suspension recovery across generations — builds ON this seam;
until the flip condition (WebUI config editing frequent enough that
restart cost hurts) it stays consumer-free.

Position (Oracle R2#7): :func:`spec_hash` and the compiler stay pure
stateless functions; the generation counter lives in
:class:`ScopeGenerationTracker`, an orchestration wrapper OUTSIDE the
compiler — the compiler itself carries zero state.
"""

from __future__ import annotations

import hashlib
from typing import Final

from modex_agent.scope.compiler import ScopeCompilation, compile_scope
from modex_agent.scope.derivation import _DEFAULT_LLM_PROVIDER
from modex_agent.scope.profile import STANDARD_PROFILES, ProfileStore
from modex_agent.scope.spec import ScopeSpec
from modex_agent.workspace.context import WorkspaceContext

_BYTE_STABLE_EXCLUDE: Final[dict[str, dict[str, dict[str, dict[str, bool]]]]] = {
    "agents": {"__all__": {"spec": {"workspace_ctx": True}}}
}
"""The lane-07 pinned exclusion contract: ``workspace_ctx`` is a runtime
object, excluded from the byte-stable serialization face (the same face
ticket 06's byte-stability test compares)."""


def spec_hash(compilation: ScopeCompilation) -> str:
    """Cross-process-stable SHA-256 over a compilation's byte-stable face.

    The hash input is the pinned serialization —
    ``ScopeCompilation.model_dump_json`` minus each agent's
    ``spec.workspace_ctx`` — which ticket 06 proved byte-identical for
    identical compile inputs: no dict-ordering or environment noise. Same
    declaration tree → same digest, in any process, under any
    ``PYTHONHASHSEED``; any spec-affecting change → a different digest.
    """
    canonical = compilation.model_dump_json(exclude=_BYTE_STABLE_EXCLUDE)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class ScopeGenerationTracker:
    """Per-pool generation counter — the stateful half of the N2 seam.

    An orchestration wrapper AROUND :func:`compile_scope` (never inside
    it): a future boot assembly sequence calls ``compile`` where it today
    calls the pure compiler directly, and each compile bumps every hosted
    pool's generation by one. ``generation`` is queryable for
    tests/logging only — the runtime consumes nothing (N2:
    restart-effective stands; hash comparison and the agent swap
    mechanism are future work, SPEC §10).
    """

    def __init__(self) -> None:
        self._generations: dict[str, int] = {}

    def compile(
        self,
        spec: ScopeSpec,
        *,
        workspace_ctx: WorkspaceContext,
        profiles: ProfileStore = STANDARD_PROFILES,
        default_llm_provider: str = _DEFAULT_LLM_PROVIDER,
    ) -> ScopeCompilation:
        """Pure compile plus one generation bump per hosted pool."""
        compilation = compile_scope(
            spec,
            workspace_ctx=workspace_ctx,
            profiles=profiles,
            default_llm_provider=default_llm_provider,
        )
        for pool in {agent.provenance.pool for agent in compilation.agents}:
            self._generations[pool] = self._generations.get(pool, 0) + 1
        return compilation

    def generation(self, pool: str) -> int:
        """A pool's compile generation; ``0`` = never compiled."""
        return self._generations.get(pool, 0)
