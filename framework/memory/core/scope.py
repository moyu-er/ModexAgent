"""Memory scope abstractions for configurable isolation dimensions."""

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Any


class MemoryAgentRole(StrEnum):
    """Agent role for memory ownership and background processing."""

    MAIN = "main"
    PEER = "peer"
    SUBAGENT = "subagent"


class MemoryLayerName(StrEnum):
    """Canonical memory layer names used in metadata and config."""

    SESSION = "session"
    ARCHIVE = "archive"
    KNOWLEDGE = "knowledge"
    PROVIDER = "provider"
    PENDING = "pending"


@dataclass
class MemoryContext:
    """统一上下文对象，包含所有可能用到的分组信息。"""

    session_id: str | None = None
    user_id: str | None = None
    tenant_id: str | None = None
    agent_id: str | None = None
    agent_role: str | MemoryAgentRole | None = None
    channel: str | None = None
    chat_id: str | None = None
    sender_agent: str | None = None
    receiver_agent: str | None = None

    def with_defaults(self, **defaults: Any) -> "MemoryContext":
        """Return a new MemoryContext with default values for missing fields."""
        current = {key: getattr(self, key) for key in [
            "session_id", "user_id", "tenant_id", "agent_id", "agent_role",
            "channel", "chat_id", "sender_agent", "receiver_agent",
        ]}
        for key, default_value in defaults.items():
            if hasattr(self, key) and current[key] is None and default_value is not None:
                current[key] = default_value
        return MemoryContext(**current)

    def to_dict(self) -> dict[str, Any]:
        """Serialize context for scope metadata."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "MemoryContext":
        """Restore context from persisted scope metadata."""
        if not data:
            return cls()
        allowed = cls.__dataclass_fields__.keys()
        return cls(**{key: data.get(key) for key in allowed})


@dataclass(frozen=True)
class ScopeRecord:
    """Recoverable metadata for a persisted memory scope."""

    scope_key: str
    layer: str | MemoryLayerName
    context: MemoryContext
    storage_path: str
    agent_role: str | MemoryAgentRole = MemoryAgentRole.MAIN
    agent_id: str | None = None
    created_at: float | None = None
    updated_at: float | None = None


def infer_agent_role(context: MemoryContext) -> MemoryAgentRole:
    """Infer role for persisted scope metadata.

    Explicit agent_id values are preferred. Unknown contexts default to main
    because ordinary single-agent use should keep full memory behavior.
    """
    candidates = [
        context.agent_role,
        context.agent_id,
        context.sender_agent,
        context.receiver_agent,
    ]
    normalized = {str(value).lower() for value in candidates if value}
    if MemoryAgentRole.SUBAGENT.value in normalized:
        return MemoryAgentRole.SUBAGENT
    if MemoryAgentRole.PEER.value in normalized:
        return MemoryAgentRole.PEER
    return MemoryAgentRole.MAIN


class MemoryScope(ABC):
    """记忆分组维度抽象基类。

    每个记忆层可以独立配置自己的 Scope，从而决定该层按什么维度隔离。
    例如：
    - 短期记忆按 SessionScope 分组
    - 长期记忆按 CompositeScope(TenantScope(), UserScope()) 分组
    - 全局人格模板按 GlobalScope 共享
    """

    @abstractmethod
    def get_scope_key(self, context: MemoryContext) -> str:
        """从上下文中提取分组键。"""
        pass

    @property
    @abstractmethod
    def name(self) -> str:
        """Scope 名称，用于调试和元数据。"""
        pass

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}()"


class SessionScope(MemoryScope):
    """按会话分组。"""

    def get_scope_key(self, context: MemoryContext) -> str:
        return context.session_id or "default"

    @property
    def name(self) -> str:
        return "session"


class UserScope(MemoryScope):
    """按用户分组。"""

    def get_scope_key(self, context: MemoryContext) -> str:
        return context.user_id or "default"

    @property
    def name(self) -> str:
        return "user"


class TenantScope(MemoryScope):
    """按租户分组。"""

    def get_scope_key(self, context: MemoryContext) -> str:
        return context.tenant_id or "default"

    @property
    def name(self) -> str:
        return "tenant"


class AgentScope(MemoryScope):
    """按 Agent 类型分组。"""

    def get_scope_key(self, context: MemoryContext) -> str:
        return context.agent_id or "default"

    @property
    def name(self) -> str:
        return "agent"


class ChannelScope(MemoryScope):
    """按频道分组（例如 IM 平台中的 channel）。"""

    def get_scope_key(self, context: MemoryContext) -> str:
        return context.channel or "default"

    @property
    def name(self) -> str:
        return "channel"


class ChatScope(MemoryScope):
    """按聊天群组分组（例如 QQ 群、微信群）。"""

    def get_scope_key(self, context: MemoryContext) -> str:
        return context.chat_id or "default"

    @property
    def name(self) -> str:
        return "chat"


class GlobalScope(MemoryScope):
    """全局共享，无视任何上下文字段。"""

    def get_scope_key(self, context: MemoryContext) -> str:
        _ = context
        return "global"

    @property
    def name(self) -> str:
        return "global"


class PeerPairScope(MemoryScope):
    """按 (conversation_id, sender_agent, receiver_agent) 三元组隔离。

    支持两种 key 构造方式：
    1. 从 session_id 解析（约定格式 '{conversation_id}:{sender}:{receiver}' 三段式）
    2. 从 MemoryContext 的独立字段构造
    """

    def __init__(self, separator: str = ":") -> None:
        self._sep = separator

    def get_scope_key(self, context: MemoryContext) -> str:
        # 方式1：session_id 已包含完整三元组（推荐，最轻量）
        if context.session_id:
            parts = context.session_id.split(self._sep)
            if len(parts) == 3:
                return context.session_id

        # 方式2：从独立字段构造
        conv_id = context.session_id or "default"
        # 如果 session_id 是两段格式（如 user↔main 的 "conv:main"），提取真正的 conversation_id
        if conv_id and self._sep in conv_id:
            parts = conv_id.split(self._sep)
            if len(parts) == 2:
                conv_id = parts[0]

        sender = context.sender_agent or context.agent_id or "unknown"
        receiver = context.receiver_agent or "unknown"
        return f"{conv_id}{self._sep}{sender}{self._sep}{receiver}"

    @property
    def name(self) -> str:
        return "peer_pair"

    @classmethod
    def create_key(
        cls,
        conversation_id: str,
        sender_agent: str,
        receiver_agent: str,
        separator: str = ":",
    ) -> str:
        """便捷方法：直接构造 scope key，无需构造 MemoryContext。"""
        return f"{conversation_id}{separator}{sender_agent}{separator}{receiver_agent}"


class CompositeScope(MemoryScope):
    """组合多个 Scope，生成复合分组键。

    例如 CompositeScope(TenantScope(), UserScope()) 会生成 "tenant_id:user_id"。
    """

    def __init__(self, *scopes: MemoryScope):
        self.scopes = scopes

    def get_scope_key(self, context: MemoryContext) -> str:
        return ":".join(s.get_scope_key(context) for s in self.scopes)

    @property
    def name(self) -> str:
        return ":".join(s.name for s in self.scopes)

    def __repr__(self) -> str:
        return f"CompositeScope({', '.join(s.name for s in self.scopes)})"
