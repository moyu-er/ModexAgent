"""Tiktoken-backed TokenEstimator for the example bot (offline vendored blob)."""
from __future__ import annotations

from modex_agent.memory.token_estimator import TokenEstimator

from bot.memory.vendor_loader import load_cl100k


class TiktokenTokenEstimator(TokenEstimator):
    """Token estimator using the cl100k_base BPE tokenizer.

    Only the text->token encoding differs from the framework default; the
    base class builds the same message payload (content, name, tool_call_id,
    tool_calls JSON) and adds ``MESSAGE_OVERHEAD``.
    """

    def __init__(self) -> None:
        self._encoding = load_cl100k()

    def estimate_text(self, text: str) -> int:
        return len(self._encoding.encode(text))
