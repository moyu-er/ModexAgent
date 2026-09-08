"""Built-in Hook implementations.

Framework-provided hooks:
- logging: RunLoggingHook
- inbox_flush: InboxFlushHook
- subagent_auto_send: SubagentAutoSendHook
- env_injection: NativeEnvInjectionHook
"""

from modex_agent.hook.builtin.current_time import CurrentTimeInjectionHook
from modex_agent.hook.builtin.env_injection import NativeEnvInjectionHook
from modex_agent.hook.builtin.inbox_flush import InboxFlushHook
from modex_agent.hook.builtin.knowledge_hook import KnowledgeHook
from modex_agent.hook.builtin.length_guard import LengthGuardHook
from modex_agent.hook.builtin.logging import RunLoggingHook
from modex_agent.hook.builtin.loop_detection import LoopDetectionHook
from modex_agent.hook.builtin.subagent_auto_send import SubagentAutoSendHook
from modex_agent.hook.builtin.todo_continuation import TodoContinuationHook
from modex_agent.hook.builtin.todo_planning_nudge import TodoPlanningNudgeHook

__all__ = [
    "CurrentTimeInjectionHook",
    "InboxFlushHook",
    "KnowledgeHook",
    "LengthGuardHook",
    "LoopDetectionHook",
    "NativeEnvInjectionHook",
    "RunLoggingHook",
    "SubagentAutoSendHook",
    "TodoContinuationHook",
    "TodoPlanningNudgeHook",
]
