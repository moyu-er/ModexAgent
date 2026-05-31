"""内置 Hook 实现。

框架预置的常用 Hook，包括：
- logging: RunLoggingHook
- runtime_context: RuntimeContextHook
- inbox_flush: InboxFlushHook
- subagent_auto_send: SubagentAutoSendHook
- subagent_cleanup: SubagentMemoryCleanupHook
- dynamic_tool_filter: DynamicToolFilterHook
- llm_output_guard: LLMOutputGuardHook
- tool_result_transform: ToolResultTransformHook
- progress_report: ProgressReportHook
- trace_writer: TraceFileWriter
"""

from framework.hook.builtin.dynamic_tool_filter import DynamicToolFilterHook
from framework.hook.builtin.inbox_flush import InboxFlushHook
from framework.hook.builtin.llm_output_guard import LLMOutputGuardHook
from framework.hook.builtin.logging import RunLoggingHook
from framework.hook.builtin.subagent_auto_send import SubagentAutoSendHook
from framework.hook.builtin.progress_report import ProgressReportHook
from framework.hook.builtin.runtime_context import RuntimeContextHook
from framework.hook.builtin.subagent_cleanup import SubagentMemoryCleanupHook
from framework.hook.builtin.tool_result_transform import ToolResultTransformHook
from framework.hook.builtin.trace_writer import TraceFileWriter

__all__ = [
    "DynamicToolFilterHook",
    "InboxFlushHook",
    "LLMOutputGuardHook",
    "SubagentAutoSendHook",
    "ProgressReportHook",
    "RunLoggingHook",
    "RuntimeContextHook",
    "SubagentMemoryCleanupHook",
    "ToolResultTransformHook",
    "TraceFileWriter",
]
