# bot/service/model_choice.py
"""Per-turn model selection's cross-broker carrier + turn-task ContextVar + BeforeGraphHook.

registry 是 session_id -> ResolvedModel 的有界 LRU：input-pipeline task 在 EnqueueStage
写入，turn task 在 ModelChoiceBindHook 读取。ContextVar 是 hook -> BotModelProvider 的同
task 桥接（asyncio.create_task 拷贝 context，task 级隔离）。

绑定点选 BEFORE_GRAPH 而非 START_NODE_TURN：approval resume 会重新进入
actual_turn()（BEFORE_GRAPH 每次都派发），而 START_NODE_TURN 只在 fresh-turn 路径
派发——resume 轮换 task 后 ContextVar 不延续，会把模型/协议选择静默回落到池默认。
"""

from __future__ import annotations

import logging
from collections import OrderedDict
from contextvars import ContextVar
from typing import TYPE_CHECKING

from modex_agent.core.capabilities import ModelInfo
from modex_agent.hook.abc import BeforeGraphHook

from .model_config import BotModelConfig, ResolvedModel

if TYPE_CHECKING:
    from modex_agent.core.agent import AgentContext

logger = logging.getLogger(__name__)

_REGISTRY_CAPACITY = 256

current_model_choice: ContextVar[ResolvedModel | None] = ContextVar(
    "current_model_choice", default=None
)


class ModelChoiceRegistry:
    """session_id -> ResolvedModel 的有界 LRU。

    不做 turn 级主动删除（会与并发 input-pipeline 写入竞态）；满时淘汰最旧。
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
    """BeforeGraphHook：把 registry 中本 session 的模型选择快照进 ContextVar，

    并把当前模型的 capabilities 覆写到 runtime.services.model_info（按 turn
    切换图片内联行为）。registry 缺失（IM / 后台）时回退默认模型。绑定发生在
    每次 actual_turn() 入口（含 approval resume），先于任何节点与 LLM/工具读取。
    """

    def __init__(self, model_config: BotModelConfig, registry: ModelChoiceRegistry) -> None:
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
            services.model_info = ModelInfo(
                model_name=resolved.model.model,
                capabilities=resolved.capabilities,
            )
