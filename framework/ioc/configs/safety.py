"""Runtime safety configuration."""

from pydantic import BaseModel


class LLMSafetyConfig(BaseModel):
    """LLM-level safety timeouts and retry settings."""

    request_timeout: float = 45.0
    stream_idle_timeout: float = 90.0
    max_retries: int = 1
    retry_backoff: list[float] = [2.0, 8.0]


class TurnSafetyConfig(BaseModel):
    """Per-turn safety timeouts."""

    agent_run_timeout: float = 180.0
    dispatch_timeout: float = 300.0
    hook_timeout: float = 10.0
    tool_timeout: float = 180.0  # Must exceed CommandTool.timeout (60s) so outer
    # interceptors never cancel the coroutine before
    # CommandTool's own timeout handling can return
    # partial output.


class SafetyConfig(BaseModel):
    """Aggregate safety configuration. None = no safety limits."""

    llm: LLMSafetyConfig = LLMSafetyConfig()
    turn: TurnSafetyConfig = TurnSafetyConfig()
