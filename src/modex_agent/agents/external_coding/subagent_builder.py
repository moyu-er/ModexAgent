"""Framework ABC for building external_coding subagent instances.

``SubagentExternalCodingBuilder`` is the seam ``AgentTemplate.materialize``
dispatches to when the subagent spec's ``execution_strategy`` is
``EXTERNAL_CODING``. It is intentionally an ABC: the framework does not know
how to assemble a concrete external-coding subagent (provider backend,
session store, env builder, …); that assembly is the business layer's
responsibility (T8 wires a concrete implementation into
``AgentMaterializeDeps.subagent_external_coding_builder``).

The contract is symmetric with the react path's ``agent_factory.create_agent``
but receives the already-constructed :class:`AgentDescriptor` plus the full
:class:`AgentMaterializeDeps` so the builder can reach pool/broker/bus without
growing a parallel parameter list. The dispatch ends with the same
``pool.register_resident`` + ``on_subagent_created`` calls the react path makes
(see ``AgentTemplate._materialize_external``), so parent-child wiring stays
uniform across execution strategies.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from modex_agent.core.session_id import SessionInfo
    from modex_agent.multi_agent.descriptor import AgentDescriptor, AgentInstance
    from modex_agent.multi_agent.materialize_deps import AgentMaterializeDeps
    from modex_agent.multi_agent.pool_config.specs import SubagentSpec


class SubagentExternalCodingBuilder(ABC):
    """Framework ABC that builds an :class:`AgentInstance` for an external_coding subagent.

    Independent of the main agent's factory path —
    :attr:`AgentMaterializeDeps.subagent_external_coding_builder` is an optional
    field, injected only by pools that declare at least one external subagent;
    react-only pools leave it ``None``.

    Implementations own the full assembly of the external-coding subagent
    (provider backend, parser, session store, env builder, harness, pipeline).
    They MUST NOT call ``pool.register_resident`` or
    ``deps.on_subagent_created`` — ``AgentTemplate._materialize_external``
    performs both after ``build`` returns, so parent-child wiring is uniform
    across the react and external-coding branches.
    """

    @abstractmethod
    async def build(
        self,
        spec: SubagentSpec,
        descriptor: AgentDescriptor,
        parent_session: SessionInfo | str | None,
        invocation_id: str | None,
        deps: AgentMaterializeDeps,
    ) -> AgentInstance:
        """Build an :class:`AgentInstance` for an external_coding subagent.

        Args:
            spec: The :class:`SubagentSpec` from the originating template —
                carries ``execution_strategy``, ``provider_kind``, ``roles``,
                ``max_steps``, and the rest of the subagent's disk projection.
            descriptor: The :class:`AgentDescriptor` already constructed by
                ``AgentTemplate._materialize_external`` with
                ``execution_strategy=EXTERNAL_CODING``, ``provider_kind`` from
                the spec, ``comm_kind=SUBAGENT``, and ``roles`` from the spec.
                The builder may extend it (e.g. wire ``context_manager``) but
                must return an :class:`AgentInstance` whose descriptor is the
                one passed in (or a value-equal replacement).
            parent_session: The parent session identity (``SessionInfo`` or
                string form). ``None`` means cold-start (no parent context).
            invocation_id: The subagent invocation id, or ``None``. Used by
                the builder to derive per-invocation provider session ids.
            deps: The full :class:`AgentMaterializeDeps` — gives the builder
                access to ``pool``, ``broker``, ``agent_bus``,
                ``workspace_path_resolver``, ``session_registry``, and the
                rest of the pool-level wiring without a parallel parameter
                list.

        Returns:
            A fully assembled :class:`AgentInstance` ready for resident
            registration. The caller (``AgentTemplate._materialize_external``)
            performs ``pool.register_resident(descriptor, instance)`` and
            ``deps.on_subagent_created(session_id, parent_session)`` after
            this returns.
        """
        ...


__all__ = ["SubagentExternalCodingBuilder"]
