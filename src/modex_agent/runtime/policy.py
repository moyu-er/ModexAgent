"""SnapshotPolicy — decides when and what to snapshot during turn execution."""

from __future__ import annotations

from abc import ABC, abstractmethod

from .enums import SnapshotReason
from .models import TurnSnapshot, TurnStateBase


class SnapshotPolicy(ABC):
    """Policy object so persistence decisions do not leak into graph nodes."""

    @abstractmethod
    def should_capture(
        self,
        state: TurnStateBase,
        reason: SnapshotReason,
    ) -> bool: ...

    @abstractmethod
    def capture(
        self,
        state: TurnStateBase,
        reason: SnapshotReason,
    ) -> TurnSnapshot: ...
