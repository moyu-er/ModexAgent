"""Stage abstraction and explicit continue/terminate results."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from framework.input_pipeline.context import InputContext
    from framework.input_pipeline.envelope import UserInputEnvelope


class StageResult(ABC):
    """Outcome of a stage. Subclasses decide via should_continue()."""

    @abstractmethod
    def should_continue(self) -> bool:
        ...

    def envelope(self) -> "UserInputEnvelope":
        raise NotImplementedError("This StageResult carries no envelope")

    @property
    def response(self) -> Any | None:
        """Optional user-facing payload (Terminate overrides)."""
        return None


@dataclass
class Continue(StageResult):
    """Continue the pipeline, carrying a (possibly modified) envelope."""

    value: "UserInputEnvelope"

    def should_continue(self) -> bool:
        return True

    def envelope(self) -> "UserInputEnvelope":
        return self.value


@dataclass
class Terminate(StageResult):
    """Terminate the pipeline early.

    response: optional payload to surface to the user (notice, error, ...).
    """

    reason: str
    response: Any | None = None

    def should_continue(self) -> bool:
        return False


class InputStage(ABC):
    """A single processing stage."""

    @abstractmethod
    async def process(
        self, envelope: "UserInputEnvelope", ctx: "InputContext"
    ) -> StageResult:
        ...
