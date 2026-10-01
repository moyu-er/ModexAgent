"""Built-in Hook implementations.

Framework-provided generic hooks:
- logging: RunLoggingHook
- current_time: CurrentTimeInjectionHook

Domain-owned hooks moved to their domains (W2): ReAct turn-lifecycle hooks
(deliver_retry, todo_continuation, todo_planning_nudge, loop_detection,
length_guard, knowledge_hook, checkpoint, env_injection) live in
``agents/react/hooks/``; ``training_data`` lives in ``trace/``.
"""

from modex_agent.hook.builtin.current_time import CurrentTimeInjectionHook
from modex_agent.hook.builtin.logging import RunLoggingHook

__all__ = [
    "CurrentTimeInjectionHook",
    "RunLoggingHook",
]
