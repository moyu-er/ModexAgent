"""BusyInputMode — how an agent handles a new message while busy.

This module previously held a runtime-control configuration aggregate that was
dead (zero readers; production wiring goes through ``AgentPipeline`` /
``PoolData`` directly) and was removed in candidate ④b.
``BusyInputMode`` is the one live export and stays.
"""

from __future__ import annotations

from enum import StrEnum


class BusyInputMode(StrEnum):
    """Agent 忙碌时收到新消息的处理模式。"""

    INTERRUPT = "interrupt"  # 中断当前 turn → 新消息
    QUEUE = "queue"  # 排队 → injection_queue
    STEER = "steer"  # 引导 → INJECT_STEER
