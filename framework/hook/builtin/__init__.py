"""内置 Hook 实现。

框架预置的常用 Hook，包括：
- logging: RunLoggingHook
- runtime_context: RuntimeContextHook
- inbox_flush: InboxFlushHook
- peer_auto_send: PeerAutoSendHook
- subagent_cleanup: SubagentMemoryCleanupHook
- dynamic_tool_filter: DynamicToolFilterHook
- tool_policy_guard: ToolPolicyGuardHook
- llm_output_guard: LLMOutputGuardHook
- tool_result_transform: ToolResultTransformHook
- progress_report: ProgressReportHook
"""

from framework.hook.builtin.dynamic_tool_filter import DynamicToolFilterHook
from framework.hook.builtin.inbox_flush import InboxFlushHook
from framework.hook.builtin.llm_output_guard import LLMOutputGuardHook
from framework.hook.builtin.logging import RunLoggingHook
from framework.hook.builtin.peer_auto_send import PeerAutoSendHook
from framework.hook.builtin.progress_report import ProgressReportHook
from framework.hook.builtin.runtime_context import RuntimeContextHook
from framework.hook.builtin.subagent_cleanup import SubagentMemoryCleanupHook
from framework.hook.builtin.tool_policy_guard import ToolPolicyGuardHook
from framework.hook.builtin.tool_result_transform import ToolResultTransformHook

__all__ = [
    "DynamicToolFilterHook",
    "InboxFlushHook",
    "LLMOutputGuardHook",
    "PeerAutoSendHook",
    "ProgressReportHook",
    "RunLoggingHook",
    "RuntimeContextHook",
    "SubagentMemoryCleanupHook",
    "ToolPolicyGuardHook",
    "ToolResultTransformHook",
]
