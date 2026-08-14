"""Runtime safety configuration."""

from pydantic import BaseModel


class LLMSafetyConfig(BaseModel):
    """LLM-level safety timeouts and retry settings.

    Defaults to None (no provider-level timeout) — the outer turn timeout
    + watchdog are the sole termination mechanism for LLM calls.
    """

    request_timeout: float | None = None
    stream_idle_timeout: float | None = None
    max_retries: int = 1
    retry_backoff: list[float] = [2.0, 8.0]


class TurnSafetyConfig(BaseModel):
    """Per-turn safety timeouts.

    ``agent_run_timeout``: per-iteration DispatchDeadline renewal amount (not
    a hard turn ceiling). See runtime/dispatch.py for the sliding ceiling design.
    ``tool_timeout``: per-invocation tool execution deadline (ToolTimeoutInterceptor).
    """

    agent_run_timeout: float = 600.0
    hook_timeout: float = 10.0
    tool_timeout: float = 400.0


class SafetyConfig(BaseModel):
    """Aggregate safety configuration. None = no safety limits."""

    llm: LLMSafetyConfig = LLMSafetyConfig()
    turn: TurnSafetyConfig = TurnSafetyConfig()
