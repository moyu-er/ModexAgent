"""Frozen task definitions for the memory e2e sentinel mini-chain (ticket 14).

``memory-chain-v1`` is the three-task sentinel chain: task 1 establishes
durable user facts while completing normal work; tasks 2 and 3 run in
brand-new sessions whose solutions structurally depend on task 1's facts, so
without persistent memory they are practically unsolvable. Each task executes
in its own container instance — only the persistent memory namespace crosses
the instance boundary.

The chain is the payload of the dual-arm ablation: the memory arm shares one
memory namespace across the chain, while the nomemory arm isolates a
per-instance namespace and clears it at the end. Arms are encoded into the
experiment name as ``memory-chain-v1.<run-id>.memory`` / ``.nomemory``
(ticket 14) so Langfuse compare can contrast the two arms directly.

This module is pure data plus validation; dual-arm orchestration lives in the
sentinel runner (plan T30).
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final, Literal

from bot.eval.task_spec import (
    FileContainsAssertion,
    FileExistsAssertion,
    WorldAssertion,
)
from pydantic import BaseModel, ConfigDict, Field

MEMORY_CHAIN_V1: Final = "memory-chain-v1"


class SentinelArm(StrEnum):
    """Ablation arms encoded into the experiment name (ticket 14)."""

    MEMORY = "memory"
    NOMEMORY = "nomemory"


def experiment_name(chain_name: str, run_id: str, arm: SentinelArm) -> str:
    """Compose the ticket-14 ablation experiment name ``{chain}.{run-id}.{arm}``."""
    if not chain_name:
        raise ValueError("chain_name must not be empty")
    if not run_id:
        raise ValueError("run_id must not be empty")
    return f"{chain_name}.{run_id}.{arm.value}"


class SentinelFact(BaseModel):
    """One durable user fact a task establishes through its normal work."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    fact_id: str = Field(min_length=1)
    statement: str = Field(min_length=1)


class ExpectedMemoryAssertion(BaseModel):
    """Proof that an earlier task's fact must be recallable in a fresh session.

    ``must_contain`` is the normalized text that appears in the dependent
    task's final answer or produced artifact only when the fact was recalled
    from persistent memory; the dependent task's prompt never contains it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    fact_id: str = Field(min_length=1)
    must_contain: str = Field(min_length=1)


class SentinelTask(BaseModel):
    """One mini-chain task: prompt, memory contract, and world-state checks."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_id: str = Field(min_length=1)
    prompt: str = Field(min_length=1)
    establishes_facts: tuple[SentinelFact, ...] = ()
    memory_assertions: tuple[ExpectedMemoryAssertion, ...] = ()
    world_assertions: tuple[WorldAssertion, ...] = ()


class SentinelChain(BaseModel):
    """A frozen mini-chain of exactly three tasks over persistent memory.

    ``fresh_session_per_task`` is structurally pinned: every task runs in a
    brand-new agent session, so only persistent memory can carry a fact from
    one task to the next.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    fresh_session_per_task: Literal[True]
    tasks: tuple[SentinelTask, ...] = Field(min_length=3, max_length=3)


class SentinelChainError(ValueError):
    """A dependency-closure violation in a sentinel mini-chain."""


def validate_chain(chain: SentinelChain) -> None:
    """Validate the cross-session dependency closure of a mini-chain.

    A chain is closed when every memory assertion references a fact that an
    earlier task establishes, the first task establishes at least one fact,
    and each later task structurally depends on a first-task fact. Anything
    else would let a fresh-session task pass without persistent memory.
    """
    task_ids = [task.task_id for task in chain.tasks]
    duplicated_ids = sorted({task_id for task_id in task_ids if task_ids.count(task_id) > 1})
    if duplicated_ids:
        raise SentinelChainError(f"duplicate task ids: {', '.join(duplicated_ids)}")

    first_task = chain.tasks[0]
    if not first_task.establishes_facts:
        raise SentinelChainError(f"task '{first_task.task_id}' must establish at least one fact")

    established_by: dict[str, int] = {}
    for index, task in enumerate(chain.tasks):
        for fact in task.establishes_facts:
            if fact.fact_id in established_by:
                raise SentinelChainError(f"fact '{fact.fact_id}' is established more than once")
            established_by[fact.fact_id] = index

    first_fact_ids = {fact.fact_id for fact in first_task.establishes_facts}

    for index, task in enumerate(chain.tasks):
        if not task.world_assertions:
            raise SentinelChainError(
                f"task '{task.task_id}' must carry at least one world assertion"
            )
        for assertion in task.memory_assertions:
            established_at = established_by.get(assertion.fact_id)
            if established_at is None:
                raise SentinelChainError(
                    f"task '{task.task_id}' references unknown fact '{assertion.fact_id}'"
                )
            if established_at >= index:
                raise SentinelChainError(
                    f"task '{task.task_id}' references fact '{assertion.fact_id}' "
                    f"established by task {established_at + 1}; "
                    f"dependency must target an earlier task"
                )
        if index == 0:
            continue
        if not task.memory_assertions:
            raise SentinelChainError(
                f"task '{task.task_id}' must structurally depend on at least one earlier fact"
            )
        if not first_fact_ids.intersection(a.fact_id for a in task.memory_assertions):
            raise SentinelChainError(
                f"task '{task.task_id}' must depend on at least one fact established by task 1"
            )


_FACT_SIGNOFF_CODENAME: Final = SentinelFact(
    fact_id="user-signoff-codename",
    statement="The user signs every document with the codename BLUEHERON.",
)

_FACT_WEEKLY_REPORT_NAME: Final = SentinelFact(
    fact_id="user-weekly-report-name",
    statement="The user's weekly report file is always named exactly voss-weekly-report.md.",
)

_TASK_ESTABLISH_CONVENTIONS: Final = SentinelTask(
    task_id="establish-conventions",
    prompt=(
        "Set up the shared conventions file for my project. Two of my standing rules: "
        "I sign every document with my personal sign-off codename BLUEHERON, and my "
        "weekly report is always saved under the exact filename voss-weekly-report.md. "
        "Create notes/onboarding.md with one short paragraph recording both rules for "
        "the team, then end the file with my sign-off codename on its own line."
    ),
    establishes_facts=(_FACT_SIGNOFF_CODENAME, _FACT_WEEKLY_REPORT_NAME),
    world_assertions=(
        FileContainsAssertion(
            kind="file_contains", path="notes/onboarding.md", content="BLUEHERON"
        ),
        FileContainsAssertion(
            kind="file_contains", path="notes/onboarding.md", content="voss-weekly-report.md"
        ),
    ),
)

_TASK_APPLY_SIGNOFF_CODENAME: Final = SentinelTask(
    task_id="apply-signoff-codename",
    prompt=(
        "Write this week's status note. Create reports/status.md containing exactly "
        "two sentences summarizing the state of the current work, then sign the file "
        "with my usual document sign-off codename on the final line."
    ),
    memory_assertions=(
        ExpectedMemoryAssertion(fact_id="user-signoff-codename", must_contain="BLUEHERON"),
    ),
    world_assertions=(
        FileContainsAssertion(kind="file_contains", path="reports/status.md", content="BLUEHERON"),
    ),
)

_TASK_APPLY_WEEKLY_REPORT_NAME: Final = SentinelTask(
    task_id="apply-weekly-report-name",
    prompt=(
        "My weekly report is due now. Create this week's report file with exactly "
        "one bullet line (starting with '- ') summarizing today's progress, and "
        "save it under the exact filename my standing conventions require for "
        "weekly reports."
    ),
    memory_assertions=(
        ExpectedMemoryAssertion(
            fact_id="user-weekly-report-name", must_contain="voss-weekly-report.md"
        ),
    ),
    world_assertions=(
        FileExistsAssertion(kind="file_exists", path="voss-weekly-report.md"),
        FileContainsAssertion(kind="file_contains", path="voss-weekly-report.md", content="-"),
    ),
)

MEMORY_CHAIN_V1_CHAIN: Final = SentinelChain(
    name=MEMORY_CHAIN_V1,
    fresh_session_per_task=True,
    tasks=(
        _TASK_ESTABLISH_CONVENTIONS,
        _TASK_APPLY_SIGNOFF_CODENAME,
        _TASK_APPLY_WEEKLY_REPORT_NAME,
    ),
)

validate_chain(MEMORY_CHAIN_V1_CHAIN)

__all__ = [
    "MEMORY_CHAIN_V1",
    "MEMORY_CHAIN_V1_CHAIN",
    "ExpectedMemoryAssertion",
    "SentinelArm",
    "SentinelChain",
    "SentinelChainError",
    "SentinelFact",
    "SentinelTask",
    "experiment_name",
    "validate_chain",
]
