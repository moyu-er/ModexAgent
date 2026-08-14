"""Agent 抽象基类和 AgentContext

提供 Agent[E] 泛型抽象基类和 AgentContext 执行上下文。
"""

from __future__ import annotations

import contextvars
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from typing_extensions import TypeVar

from modex_agent.core.history import MessageHistory
from modex_agent.core.session_id import SessionInfo

from .emitter import AgentResult, ContentEmitter
from .events import AgentEvent
from .message_utils import normalize_agent_messages_for_llm
from .tool_manager import ToolManager

if TYPE_CHECKING:
    from modex_agent.core.capabilities import ModelCapabilities
    from modex_agent.core.prompt import SystemPromptPipeline
    from modex_agent.pipeline.snapshot import PoolDataSnapshot
    from modex_agent.runtime.models import TurnIdentity
    from modex_agent.runtime.services import AgentRuntime
    from modex_graph.context import GraphContext


class AgentCommKind(StrEnum):
    """Agent topology kind — internal routing classification.

    ``NORMAL`` is the internal implementation term for a main/peer agent.
    Agent-facing text (tool descriptions, message bodies) uses **peer**,
    not "normal" — e.g. "Peer targets", "Message from peer agent 'X'".
    """

    NORMAL = "normal"
    SUBAGENT = "subagent"


class AgentImplementation(StrEnum):
    """How an agent is implemented — orthogonal to :class:`AgentCommKind`.

    Topology (NORMAL vs SUBAGENT) decides routing and reply topology.
    Implementation decides how the agent actually runs and how peers
    should word the reply contract (``task`` tool vs ``modexctl
    send`` CLI vs future mechanisms).

    Combinations are valid:
    - NORMAL + NATIVE = modex main agent (default/coder pool main)
    - NORMAL + EXTERNAL = external coding CLI as pool main (opencode/pi)
    - SUBAGENT + NATIVE = modex subagent (the only subagent shape today)
    - SUBAGENT + EXTERNAL = supported (external CLI as subagent, ADR-0027)
    """

    NATIVE = "native"
    EXTERNAL = "external"


@dataclass
class AgentContext:
    """Agent execution context — typed runtime state via ``runtime`` field."""

    system_prompt: str
    history: MessageHistory
    """Message history for the current session.

    **IMPORTANT**: Do NOT use ``len(history)``, ``list(history)``, or index
    access ``history[0]`` — these are **not** guaranteed to work.  The pool-mode
    implementation (async-backed history) intentionally raises on
    synchronous ``__len__`` / ``__iter__`` / ``__getitem__`` because messages
    live in an async storage backend.

    Always use ``await history.to_list()`` to get a list of messages, then use
    ``len()`` / iteration on *that* list::

        messages = await ctx.history.to_list()
        count = len(messages)
        for msg in messages:
            ...
    """
    tool_manager: ToolManager
    session: SessionInfo
    comm_kind: AgentCommKind | None = None
    max_iterations: int = 10
    # TODO(model-config-convergence): 模型调用参数 temperature/max_output_tokens 应只由
    # LLMProvider 持有；此处经 descriptor/context 透传属冗余复制。待 ReactLlmClient
    # 不再传这两参后，本字段/参数可连同 AgentContext.temperature/max_output_tokens、
    # AgentLLMConfig、AgentMaterializeDeps 的同名字段一并删除。收敛目标见
    # docs/superpowers/plans/2026-07-03-bot-multi-model.md §框架配置收敛后续。
    temperature: float | None = None
    max_output_tokens: int | None = None
    attachments: list[str] = field(default_factory=list)
    emitter: ContentEmitter | None = None
    runtime: AgentRuntime | None = None
    graph_context: GraphContext[Any] | None = None
    graph_instance_id: int | None = None
    """Graph instance this turn belongs to, or None for non-graph turns.

    Set by ``GraphContextBindingConfigurator`` from ``TurnContextDescriptor``.
    Propagated through 5 injection sites: SendStrategy (main→subagent),
    SubagentAutoSendHook (subagent→parent), ExternalTurnRunner (metadata→ctx),
    modexctl SendRequest/facade (CLI→ctx), and ReActTurnRunner._build_turn_descriptor
    (metadata→descriptor→configurator).
    """
    identity: TurnIdentity | None = None
    system_prompt_pipeline: SystemPromptPipeline | None = None
    workspace_snapshot: PoolDataSnapshot | None = None
    """Per-turn data snapshot resolved from the active Workspace at turn start.

    Hooks and agents that need workspace-scoped data (e.g. the experience dir)
    read it from here. None when no workspace manager is wired, in which case
    consumers fall back to their own defaults.
    """

    workspace: Path | None = None
    """Working directory for the current turn, from ``InputMessage.workspace``.

    Carried so inter-agent communication strategies can propagate it in the
    envelope payload. ``None`` when no workspace is bound.
    """

    current_input: str | None = None
    """The sanitized user input for the current turn.

    External coding agents read this directly instead of mining history.
    None for ReAct agents (they use history); set by ``ReActTurnRunner``
    after ``build_runtime_and_context`` returns, from the turn's
    ``sanitized_content``.
    """

    async def get_resolved_system_prompt(self) -> str:
        """Return the system prompt the LLM actually receives.

        Single source of truth for all consumers (LLMNode, ChatSpanHook,
        AgentStartSpanHook): pipeline first (full 15-provider assembly
        including GraphWorkflowProvider content), static ``system_prompt``
        field as fallback when no pipeline or pipeline returns empty.

        Must NOT be replaced by direct ``system_prompt`` field reads —
        the field holds only the 3-provider static fallback
        (runtime + base + core_memory), not the full pipeline output.
        """
        if self.system_prompt_pipeline is not None:
            content = await self.system_prompt_pipeline.get_or_refresh()
            if content:
                return content
        return self.system_prompt

    @property
    def current_turn_uuid(self) -> str | None:
        """Current turn UUID from runtime state, for control command validation."""
        if self.runtime is None:
            return None
        return self.runtime.turn_uuid

    async def to_messages(self) -> list[dict[str, Any]]:
        raw_history = await self.history.to_list()
        history_list = normalize_agent_messages_for_llm(raw_history)
        non_system = [msg for msg in history_list if msg.get("role") != "system"]

        def _strip_none(d: dict[str, Any]) -> dict[str, Any]:
            return {k: v for k, v in d.items() if v is not None}

        return [_strip_none(msg) for msg in non_system]

    def get_tool_descriptions(self) -> list[dict[str, Any]]:
        caps: ModelCapabilities | None = (
            self.runtime.model_info.capabilities
            if self.runtime is not None and self.runtime.model_info is not None
            else None
        )
        return self.tool_manager.get_tool_descriptions(caps)

    def add_attachment(self, path: str) -> None:
        self.attachments.append(path)


current_agent_context: contextvars.ContextVar[AgentContext] = contextvars.ContextVar(
    "current_agent_context"
)

E = TypeVar("E", bound=AgentEvent)


class Agent[E: AgentEvent](ABC):
    """Agent 推理模式抽象基类

    职责：执行特定的推理模式（ReAct、Plan 等）。
    不处理：消息路由、历史管理、输出发送。

    通过 ContentEmitter 输出内容，不关心外部如何处理。

    每个子类应定义 event_enum，说明该 Agent 会触发哪些事件类型。

    泛型参数 E 是 Agent 特定的事件枚举类型。
    """

    # 子类必须定义使用的事件类型枚举
    # 例如：event_enum = ReActEvent
    event_enum: type[E] = None  # type: ignore

    @abstractmethod
    async def run(
        self,
        context: AgentContext,
        emitter: ContentEmitter[E],
    ) -> AgentResult:
        """执行 Agent

        Args:
            context: 执行上下文
            emitter: 内容发送器（类型参数与该 Agent 的事件枚举匹配）

        Returns:
            AgentResult: 执行结果
        """
        pass

    @property
    @abstractmethod
    def name(self) -> str:
        """Agent 名称"""
        pass

    @classmethod
    def get_event_enum(cls) -> type[E]:
        """获取该 Agent 使用的事件类型枚举

        外部可以根据这个信息配置 Emitter。

        Returns:
            该 Agent 的事件枚举类（如 ReActEvent）
        """
        return cls.event_enum

    async def stop(self) -> None:
        """释放 Agent 持有的资源（子进程、网络连接等）。

        默认实现是空操作。持有外部资源的子类（如
        :class:`ExternalAgent` 管理 ``opencode serve`` 子进程）
        应覆盖此方法。Pool shutdown 时通过
        :meth:`AgentInstance.stop` → ``pipeline.agent.stop()`` 调用。
        """
