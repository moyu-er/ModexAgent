"""Compression coordinator factory — builds DefaultMemoryCompressionCoordinator
from IOC MemoryConfig.
"""

from __future__ import annotations

from typing import Any

from framework.ioc.configs.memory import MemoryConfig


def create_subagent_compression_coordinator(
    cfg: MemoryConfig | None,
) -> Any | None:
    """Build a lightweight compression coordinator for peers/subagents.

    Peers only get basic token/message thresholds; no LLM summarizer.

    summary=None is intentional: peers operate session-only (no archive),
    so archive writes are skipped and no summary is ever needed.
    """
    if cfg is None:
        return None

    st = cfg.short_term
    from framework.memory.compression.policies import DefaultMemoryCompressionCoordinator

    return DefaultMemoryCompressionCoordinator(
        max_messages=st.max_messages,
        max_tokens=st.max_tokens,
        keep_ratio_for_messages=st.keep_ratio_for_messages,
        keep_ratio_for_token=st.keep_ratio_for_token,
    )
