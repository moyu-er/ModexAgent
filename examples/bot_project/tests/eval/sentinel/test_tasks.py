"""Tests for the memory e2e sentinel mini-chain definitions (ticket 14)."""

from __future__ import annotations

import pytest
from evals.sentinel.tasks import (
    MEMORY_CHAIN_V1,
    MEMORY_CHAIN_V1_CHAIN,
    SentinelArm,
    SentinelChain,
    SentinelChainError,
    experiment_name,
    validate_chain,
)
from pydantic import ValidationError


def _payload() -> dict[str, object]:
    """Return a fresh JSON-mode dump of the shipped chain payload."""
    return MEMORY_CHAIN_V1_CHAIN.model_dump(mode="json")


def test_chain_is_frozen_three_task_memory_chain_v1() -> None:
    # Given: the shipped sentinel mini-chain.
    # Then: it carries the ticket-14 chain name, exactly three tasks, and the
    # fresh-session-per-task ablation structure.
    assert MEMORY_CHAIN_V1 == "memory-chain-v1"
    assert MEMORY_CHAIN_V1_CHAIN.name == MEMORY_CHAIN_V1
    assert MEMORY_CHAIN_V1_CHAIN.fresh_session_per_task is True
    assert len(MEMORY_CHAIN_V1_CHAIN.tasks) == 3
    task_ids = [task.task_id for task in MEMORY_CHAIN_V1_CHAIN.tasks]
    assert len(set(task_ids)) == 3


def test_first_task_establishes_durable_facts() -> None:
    # Given: the first task of the chain.
    first = MEMORY_CHAIN_V1_CHAIN.tasks[0]

    # Then: it establishes at least one durable fact through normal work,
    # recalls nothing, and carries world-state checks.
    assert first.prompt
    assert len(first.establishes_facts) >= 1
    assert len({fact.fact_id for fact in first.establishes_facts}) == len(first.establishes_facts)
    assert first.memory_assertions == ()
    assert len(first.world_assertions) >= 1


def test_dependent_tasks_reference_first_task_facts_without_leaking_values() -> None:
    # Given: tasks 2 and 3 run in brand-new sessions.
    first_fact_ids = {fact.fact_id for fact in MEMORY_CHAIN_V1_CHAIN.tasks[0].establishes_facts}

    for task in MEMORY_CHAIN_V1_CHAIN.tasks[1:]:
        # Then: each structurally depends on task 1's facts, establishes none
        # itself, and its prompt never leaks the recall proof text.
        assert task.establishes_facts == ()
        assert len(task.memory_assertions) >= 1
        assert len(task.world_assertions) >= 1
        for assertion in task.memory_assertions:
            assert assertion.fact_id in first_fact_ids
            assert assertion.must_contain not in task.prompt


def test_validate_chain_accepts_memory_chain_v1() -> None:
    # Given: the shipped chain. When: validated. Then: closure holds (no raise).
    validate_chain(MEMORY_CHAIN_V1_CHAIN)


def test_validate_chain_rejects_unknown_fact_reference() -> None:
    # Given: task 2 references a fact id no task establishes.
    payload = _payload()
    tasks = payload["tasks"]
    assert isinstance(tasks, list)
    tasks[1]["memory_assertions"] = [{"fact_id": "ghost-fact", "must_contain": "BLUEHERON"}]
    chain = SentinelChain.model_validate(payload)

    # When / Then: dependency closure is broken and the unknown id is named.
    with pytest.raises(SentinelChainError, match="ghost-fact"):
        validate_chain(chain)


def test_validate_chain_rejects_non_earlier_fact_reference() -> None:
    # Given: task 2 references a fact it establishes itself in the same task.
    payload = _payload()
    tasks = payload["tasks"]
    assert isinstance(tasks, list)
    tasks[1]["establishes_facts"] = [{"fact_id": "own-fact", "statement": "Self."}]
    tasks[1]["memory_assertions"] = [{"fact_id": "own-fact", "must_contain": "BLUEHERON"}]
    chain = SentinelChain.model_validate(payload)

    # When / Then: a same-task reference is not a cross-session dependency.
    with pytest.raises(SentinelChainError, match="earlier task"):
        validate_chain(chain)


def test_validate_chain_rejects_dependent_task_without_memory_assertions() -> None:
    # Given: task 3 carries no memory assertion at all.
    payload = _payload()
    tasks = payload["tasks"]
    assert isinstance(tasks, list)
    tasks[2]["memory_assertions"] = []
    chain = SentinelChain.model_validate(payload)

    # When / Then: a later task without structural dependency is rejected.
    with pytest.raises(SentinelChainError, match="structurally depend"):
        validate_chain(chain)


def test_validate_chain_rejects_later_task_without_first_task_dependency() -> None:
    # Given: task 3 depends only on a fact task 2 establishes, so closure over
    # earlier tasks holds but the ticket-14 dependency on task 1 does not.
    payload = _payload()
    tasks = payload["tasks"]
    assert isinstance(tasks, list)
    tasks[1]["establishes_facts"] = [{"fact_id": "mid-fact", "statement": "Mid."}]
    tasks[2]["memory_assertions"] = [{"fact_id": "mid-fact", "must_contain": "BLUEHERON"}]
    chain = SentinelChain.model_validate(payload)

    # When / Then: every later task must need a task-1 fact.
    with pytest.raises(SentinelChainError, match="task 1"):
        validate_chain(chain)


def test_validate_chain_rejects_first_task_without_facts() -> None:
    # Given: task 1 establishes nothing durable.
    payload = _payload()
    tasks = payload["tasks"]
    assert isinstance(tasks, list)
    tasks[0]["establishes_facts"] = []
    chain = SentinelChain.model_validate(payload)

    # When / Then: the establisher role of task 1 is mandatory.
    with pytest.raises(SentinelChainError, match="establish at least one fact"):
        validate_chain(chain)


def test_validate_chain_rejects_duplicate_task_ids() -> None:
    # Given: two tasks share one task id.
    payload = _payload()
    tasks = payload["tasks"]
    assert isinstance(tasks, list)
    tasks[1]["task_id"] = tasks[0]["task_id"]
    chain = SentinelChain.model_validate(payload)

    # When / Then: duplicate task ids are rejected.
    with pytest.raises(SentinelChainError, match="duplicate task id"):
        validate_chain(chain)


def test_validate_chain_rejects_duplicate_fact_ids() -> None:
    # Given: task 2 establishes a fact id task 1 already established.
    payload = _payload()
    tasks = payload["tasks"]
    assert isinstance(tasks, list)
    first_fact_id = tasks[0]["establishes_facts"][0]["fact_id"]
    tasks[1]["establishes_facts"] = [{"fact_id": first_fact_id, "statement": "Duplicated."}]
    chain = SentinelChain.model_validate(payload)

    # When / Then: ambiguous fact ownership is rejected.
    with pytest.raises(SentinelChainError, match="more than once"):
        validate_chain(chain)


def test_validate_chain_rejects_task_without_world_assertions() -> None:
    # Given: task 2 carries no world-state check, so it has no verifiable verdict.
    payload = _payload()
    tasks = payload["tasks"]
    assert isinstance(tasks, list)
    tasks[1]["world_assertions"] = []
    chain = SentinelChain.model_validate(payload)

    # When / Then: a verdict-less task is rejected.
    with pytest.raises(SentinelChainError, match="world assertion"):
        validate_chain(chain)


def test_experiment_name_composes_ticket14_arm_names() -> None:
    # Given: the ticket-14 naming pattern {chain}.{run-id}.{arm}.
    # When / Then: both ablation arms compose the exact experiment names.
    assert (
        experiment_name(MEMORY_CHAIN_V1, "run-42", SentinelArm.MEMORY)
        == "memory-chain-v1.run-42.memory"
    )
    assert (
        experiment_name(MEMORY_CHAIN_V1, "run-42", SentinelArm.NOMEMORY)
        == "memory-chain-v1.run-42.nomemory"
    )


def test_experiment_name_rejects_empty_segments() -> None:
    # Given: empty chain or run-id segments.
    # When / Then: the composed experiment name would be malformed, so it raises.
    with pytest.raises(ValueError, match="chain_name"):
        experiment_name("", "run-42", SentinelArm.MEMORY)
    with pytest.raises(ValueError, match="run_id"):
        experiment_name(MEMORY_CHAIN_V1, "", SentinelArm.MEMORY)


def test_models_are_frozen() -> None:
    # Given: the shipped chain value object.
    # When / Then: field reassignment is rejected by the frozen contract.
    with pytest.raises(ValidationError, match="Instance is frozen"):
        MEMORY_CHAIN_V1_CHAIN.tasks[0].prompt = "rewritten"  # type: ignore[index]


def test_models_reject_extra_fields() -> None:
    # Given: a task payload carrying an undeclared field.
    payload = _payload()
    tasks = payload["tasks"]
    assert isinstance(tasks, list)
    tasks[0]["unexpected"] = True

    # When / Then: strict Pydantic parsing rejects the extra field.
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        SentinelChain.model_validate(payload)


def test_fresh_session_per_task_is_structurally_enforced() -> None:
    # Given: a payload that reuses sessions across tasks.
    payload = _payload()
    payload["fresh_session_per_task"] = False

    # When / Then: the ablation structure is rejected at parse time.
    with pytest.raises(ValidationError):
        SentinelChain.model_validate(payload)


def test_chain_rejects_wrong_task_count() -> None:
    # Given: payloads with two and four tasks.
    short_payload = _payload()
    stretched_payload = _payload()
    short_tasks = short_payload["tasks"]
    stretched_tasks = stretched_payload["tasks"]
    assert isinstance(short_tasks, list)
    assert isinstance(stretched_tasks, list)
    short_payload["tasks"] = short_tasks[:2]
    stretched_payload["tasks"] = stretched_tasks + [stretched_tasks[0]]

    # When / Then: the mini-chain is exactly three tasks.
    with pytest.raises(ValidationError):
        SentinelChain.model_validate(short_payload)
    with pytest.raises(ValidationError):
        SentinelChain.model_validate(stretched_payload)


def test_chain_round_trips_through_serialization() -> None:
    # Given: the shipped chain.
    # When: dumped to JSON-compatible data and revalidated.
    round_tripped = SentinelChain.model_validate(MEMORY_CHAIN_V1_CHAIN.model_dump(mode="json"))

    # Then: the frozen definition is byte-stable across serialization.
    assert round_tripped == MEMORY_CHAIN_V1_CHAIN
