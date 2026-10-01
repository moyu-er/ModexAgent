# app/models/choice.py
"""Per-turn model selection's cross-broker carrier + turn-task ContextVar + BeforeGraphHook.

The registry is a bounded LRU of session_id -> ResolvedModel: the
input-pipeline task writes it at EnqueueStage, the turn task reads it in
ModelChoiceBindHook. The ContextVar is the same-task bridge from the hook to
ModelSelectionProvider (asyncio.create_task copies the context; per-task
isolation).

The bind point is BEFORE_GRAPH, not START_NODE_TURN: an approval resume
re-enters actual_turn() (BEFORE_GRAPH dispatches every time), while
START_NODE_TURN dispatches only on the fresh-turn path — after a resume
rotates the task, the ContextVar does not carry over and the model/protocol
selection would silently fall back to the pool default.
"""

from __future__ import annotations

import logging
from collections import OrderedDict
from contextvars import ContextVar
from typing import TYPE_CHECKING

from modex_agent.app.models.registry import ModelRegistry, ResolvedModel
from modex_agent.hook.abc import BeforeGraphHook

if TYPE_CHECKING:
    from modex_agent.core.agent import AgentContext

logger = logging.getLogger(__name__)

_REGISTRY_CAPACITY = 256

current_model_choice: ContextVar[ResolvedModel | None] = ContextVar(
    "current_model_choice", default=None
)


class ModelChoiceRegistry:
    """A bounded LRU of session_id -> ResolvedModel.

    No turn-level proactive deletion (it would race concurrent input-pipeline
    writes); when full, evict the oldest.
    """

    def __init__(self, capacity: int = _REGISTRY_CAPACITY) -> None:
        self._capacity = capacity
        self._store: OrderedDict[str, ResolvedModel] = OrderedDict()

    def set(self, session_id: str, resolved: ResolvedModel) -> None:
        if session_id in self._store:
            self._store.move_to_end(session_id)
            self._store[session_id] = resolved
            return
        self._store[session_id] = resolved
        while len(self._store) > self._capacity:
            self._store.popitem(last=False)

    def get(self, session_id: str) -> ResolvedModel | None:
        if session_id not in self._store:
            return None
        self._store.move_to_end(session_id)
        return self._store[session_id]

    def __len__(self) -> int:
        return len(self._store)


class ModelChoiceBindHook(BeforeGraphHook):
    """BeforeGraphHook: snapshots this session's model selection from the registry into the ContextVar,

    and overwrites runtime.services.model_info with the current model's
    profile (capabilities + context_limit/max_output_tokens budgets)
    (switching image-inlining/budget behavior per turn). When the registry
    misses (IM / background), fall back to the default model. Binding happens
    at every actual_turn() entry (including approval resumes), before any
    node and LLM/tool reads.
    """

    def __init__(self, model_config: ModelRegistry, registry: ModelChoiceRegistry) -> None:
        self._model_config = model_config
        self._registry = registry

    @property
    def name(self) -> str:
        return "model_choice_bind_hook"

    async def before_graph(self, ctx: AgentContext) -> None:
        session_id = ctx.session.session_id if ctx.session is not None else ""
        resolved = self._registry.get(session_id) if session_id else None
        if resolved is None:
            resolved = self._model_config.default_resolved()
        current_model_choice.set(resolved)
        runtime = ctx.runtime
        services = runtime.services if runtime is not None else None
        if services is not None:
            services.model_info = resolved.model_info
