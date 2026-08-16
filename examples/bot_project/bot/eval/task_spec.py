"""Typed contract for multi-turn eval tasks and their expected world state.

The schema selects a framework tool preset, applies an optional per-case tool
deny-list, and describes world assertions for a later runner to execute. The
framework ``READ_ONLY`` preset is not a write-ablation: it includes the shell
tool (``modex_agent.tools.presets`` lines 61 and 163), while tool filtering is
by name only (``modex_agent.tools.filter`` line 25). True ablations therefore
compose ``toolset=NONE`` or ``deny_tools``.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field


class FileExistsAssertion(BaseModel):
    """Require a path to exist after the eval task."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["file_exists"]
    path: str


class FileAbsentAssertion(BaseModel):
    """Require a path to be absent after the eval task."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["file_absent"]
    path: str


class FileContainsAssertion(BaseModel):
    """Require a file to contain the specified text after the eval task."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["file_contains"]
    path: str
    content: str


class CommandExitAssertion(BaseModel):
    """Require a command to finish with the expected exit code."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["command_exit"]
    command: list[str] = Field(min_length=1)
    expected_exit: int = 0


WorldAssertion = Annotated[
    FileExistsAssertion
    | FileAbsentAssertion
    | FileContainsAssertion
    | CommandExitAssertion,
    Field(discriminator="kind"),
]


class EvalTurn(BaseModel):
    """One user turn and its optional expected stop reason."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    user: str
    expected_stop: str | None = None


class EvalToolset(StrEnum):
    """Closed toolset selections resolved by the later eval runner."""

    NONE = "none"
    READ_ONLY = "read_only"
    READ_WRITE = "read_write"
    FULL = "full"


class EvalItemSpec(BaseModel):
    """Validated multi-turn task consumed by the eval runner v2."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    turns: list[EvalTurn] = Field(min_length=1)
    toolset: EvalToolset = EvalToolset.FULL
    deny_tools: list[str] = Field(default_factory=list)
    world_setup: dict[str, str] = Field(default_factory=dict)
    world_assertions: list[WorldAssertion] = Field(default_factory=list)
    metadata: dict[str, str] = Field(default_factory=dict)

    @classmethod
    def from_item_input(cls, raw: object) -> EvalItemSpec | None:
        """Parse v2 input while signaling legacy input with ``None``."""
        if isinstance(raw, dict) and "turns" in raw:
            return cls.model_validate(raw)
        return None


__all__ = [
    "CommandExitAssertion",
    "EvalItemSpec",
    "EvalToolset",
    "EvalTurn",
    "FileAbsentAssertion",
    "FileContainsAssertion",
    "FileExistsAssertion",
    "WorldAssertion",
]
