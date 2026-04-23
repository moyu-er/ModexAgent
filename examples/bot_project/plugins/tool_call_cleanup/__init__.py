"""tool_call_cleanup —— Tool-Call 感知短期记忆管理插件。

本插件在 ReAct Agent 完成一轮正常对话后，自动从短期记忆中清理
所有 tool-call 中间步骤（含 tool_calls 的 assistant + tool 结果消息），
只保留 user 输入和最终 assistant 回复。

同时处理上轮 ReAct 达到迭代上限中断的场景：
- 在用户继续输入并完成新 ReAct 后，移除上轮残留的 assistant-tool 对
- 插入模拟 assistant 消息说明上轮被强制终止
- 压缩时若仍有 tool 消息残留，截断而非压缩

架构
====
    ┌───────────────────────────────────┐
    │ ToolCallAwareShortTermManager     │  ← 继承 ShortTermMemoryManager
    │  (extends ShortTermMemoryManager) │     重写 add_messages / _maybe_compress
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

不需要业务代码硬编码任何注入逻辑。
"""

import logging
from typing import TYPE_CHECKING

from framework.plugins.context import PluginContext

from .manager import ToolCallAwareShortTermManager
from .policy import ToolCallCleanupPolicy

if TYPE_CHECKING:
    from framework.memory.system import MemorySystem

logger = logging.getLogger(__name__)

__all__ = [
    "ToolCallCleanupPolicy",
    "ToolCallAwareShortTermManager",
]


def _inject(memory_system: "MemorySystem") -> None:
    """Modifier：将 ToolCallAwareShortTermManager 注入到 MemorySystem。

    被 PluginLoader.inject_memory_system_modifiers() 调用，
    在 MemorySystem.initialize() 之后执行。

    从现有的 ShortTermMemoryManager 提取参数，构造自定义子类实例，
    替换 MemorySystemManagers 中的 short_term 字段。
    """
    original = memory_system._managers.short_term

    # 安全类型检查：确保原始 manager 是 ShortTermMemoryManager 或子类
    from framework.memory.managers.short_term import ShortTermMemoryManager

    if not isinstance(original, ShortTermMemoryManager):
        logger.warning(
            "short_term manager is %s, not ShortTermMemoryManager, skipping",
            type(original).__name__,
        )
        return

    memory_system._managers.short_term = ToolCallAwareShortTermManager(
        storage=original._storage,
        scope=original._scope,
        config=original._config,
        history_manager=original._history_manager,
    )
    logger.info("Injected ToolCallAwareShortTermManager into MemorySystem")


def register(ctx: PluginContext) -> None:
    """插件注册入口。

    通过 register_memory_system_modifier 注册 modifier，
    让插件系统自动在 MemorySystem 初始化后注入自定义 manager。
    """
    enabled = ctx.get_config("enabled", True)
    if not enabled:
        logger.info("tool_call_cleanup plugin disabled by config")
        return

    ctx.register_memory_system_modifier(_inject)
    logger.info("Registered tool_call_cleanup memory_system modifier")
