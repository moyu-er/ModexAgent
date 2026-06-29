"""Shared fixtures for memory tests."""
from __future__ import annotations

from modex_agent.memory.token_estimator import TokenEstimator


class FixedTokenEstimator(TokenEstimator):
    """Deterministic estimator: estimate_text returns ``per_message`` (default 10).

    estimate_message therefore returns per_message + MESSAGE_OVERHEAD (4) per message.
    """

    def __init__(self, per_message: int = 10) -> None:
        self.per_message = per_message

    def estimate_text(self, text: str) -> int:
        return self.per_message
