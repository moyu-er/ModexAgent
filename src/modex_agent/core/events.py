"""Agent 事件基类和 Emitter 配置

提供 AgentEvent ABC 和 EmitterConfig（frozen Pydantic），支持事件过滤。
"""

from pydantic import BaseModel, ConfigDict, Field


class AgentEvent:
    """Agent 事件基类（标记类）

    所有 Agent 特定的事件枚举都应继承此类。
    用于类型约束和统一接口。

    注意：此类不使用 ABC，以避免与 Enum 的 metaclass 冲突。
    """

    pass


class EmitterConfig(BaseModel):
    """Emitter 配置

    控制哪些事件类型会被发送，避免不必要的处理和传输。
    使用字符串事件名，支持跨 Agent 类型复用。

    示例:
        # ReActAgent 配置
        config = EmitterConfig(
            enabled_events={"model_output", "final_output"}
        )

        # PlanAgent 配置
        config = EmitterConfig(
            disabled_events={"plan_revision"}
        )
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    # 启用的事件类型（None 表示全部启用）
    enabled_events: frozenset[str] | None = None

    # 禁用的事件类型（优先级高于 enabled_events）
    disabled_events: frozenset[str] = Field(default_factory=frozenset)

    def is_enabled(self, event_name: str) -> bool:
        """检查事件是否启用

        Args:
            event_name: 要检查的事件名称（字符串）

        Returns:
            如果事件应该被处理返回 True，否则返回 False
        """
        if event_name in self.disabled_events:
            return False
        if self.enabled_events is not None:
            return event_name in self.enabled_events
        return True
