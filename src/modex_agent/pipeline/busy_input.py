"""BusyInputMode — how an agent handles a new message while busy.

Moved from core/agent_runtime_config.py (plan §15 B2); the former
runtime-control configuration aggregate in that module was dead (zero
readers) and had been removed earlier (ADR-0006 candidate ④b).
"""

from __future__ import annotations

from enum import StrEnum


class BusyInputMode(StrEnum):
    """Agent 忙碌时收到新消息的处理模式。"""

    INTERRUPT = "interrupt"  # 中断当前 turn → 新消息
    QUEUE = "queue"  # 排队 → injection_queue
    STEER = "steer"  # 引导 → INJECT_STEER
