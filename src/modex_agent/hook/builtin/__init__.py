"""Built-in Hook implementations.

Framework-provided hooks:
- experience_review: ExperienceReviewHook
- logging: RunLoggingHook
- runtime_context: RuntimeContextHook
- inbox_flush: InboxFlushHook
- subagent_auto_send: SubagentAutoSendHook
- env_injection: NativeEnvInjectionHook
"""

from modex_agent.hook.builtin.env_injection import NativeEnvInjectionHook
from modex_agent.hook.builtin.experience_review import ExperienceReviewHook
from modex_agent.hook.builtin.inbox_flush import InboxFlushHook
from modex_agent.hook.builtin.logging import RunLoggingHook
from modex_agent.hook.builtin.loop_detection import LoopDetectionHook
from modex_agent.hook.builtin.runtime_context import RuntimeContextHook
from modex_agent.hook.builtin.subagent_auto_send import SubagentAutoSendHook

__all__ = [
    "ExperienceReviewHook",
    "InboxFlushHook",
    "LoopDetectionHook",
    "NativeEnvInjectionHook",
    "RunLoggingHook",
    "RuntimeContextHook",
    "SubagentAutoSendHook",
]
