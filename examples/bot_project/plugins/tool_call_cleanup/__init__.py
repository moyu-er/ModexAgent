"""tool_call_cleanup -- Tool-Call 感知 Session 记忆管理插件。

本插件在 ReAct Agent 完成一轮正常对话后，自动从 Session 记忆中清理
所有 tool-call 中间步骤（含 tool_calls 的 assistant + tool 结果消息），
只保留 user 输入和最终 assistant 回复。

同时处理上轮 ReAct 达到迭代上限中断的场景：
- 在用户继续输入并完成新 ReAct 后，移除上轮残留的 assistant-tool 对
- 压缩时若仍有 tool 消息残留，截断而非压缩

架构
====
    ┌───────────────────────────────────┐
    │ ToolCallAwareSessionManager       │  ← 实现 SessionMemoryManager (装饰器模式)
    │  wraps inner SessionMemoryManager │     委托 + 写入后清理
    └─────────────┬─────────────────────┘
                  │ uses
                  ▼
    ┌───────────────────────────────────┐
    │ ToolCallCleanupPolicy             │  ← 纯策略类 (policy.py)
    └───────────────────────────────────┘

注入方式
--------
通过插件系统自动发现和注入：

1. PluginManager.discover_and_load() 扫描 plugins/ 目录
2. 调用本模块的 register(ctx)
3. ctx.register_memory_system_modifier() 注册 modifier
4. PluginLoader.inject_memory_system_modifiers() 应用到 MemorySystem
5. modifier 通过 MemoryLayerSet.with_session() 替换 session 管理器

不需要业务代码硬编码任何注入逻辑。
"""

import logging
from typing import Any

from modex_agent.plugins.context import PluginContext

from .manager import ToolCallAwareSessionManager
from .policy import ToolCallCleanupPolicy

logger = logging.getLogger(__name__)

__all__ = [
    "ToolCallCleanupPolicy",
    "ToolCallAwareSessionManager",
]


def _inject(memory_system: Any) -> None:
    """Modifier: wrap the session manager with tool-call cleanup."""
    original = memory_system.layers.session
    wrapped = ToolCallAwareSessionManager(
        inner=original,
        policy=ToolCallCleanupPolicy(),
    )
    memory_system._layers = memory_system.layers.with_session(wrapped)
    logger.info("Injected ToolCallAwareSessionManager into MemorySystem")


def register(ctx: PluginContext) -> None:
    """插件注册入口。"""
    enabled = ctx.get_config("enabled", True)
    if not enabled:
        logger.info("tool_call_cleanup plugin disabled by config")
        return
    ctx.register_memory_system_modifier(_inject)
    logger.info("Registered tool_call_cleanup memory_system modifier")
