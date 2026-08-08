"""Tests for the frozen Pydantic value objects in bot.kb.models."""

from __future__ import annotations

import pytest
from bot.kb.models import (
    KbAction,
    KbControlRequest,
    KbEntry,
    KbFilter,
    KbSearchResult,
    KbUpsertRequest,
)
from pydantic import ValidationError

# ── KbFilter ──────────────────────────────────────────────────────────


def test_filter_defaults_all_none_when_no_args() -> None:
    """KbFilter() with no args leaves every dimension as None (global)."""
    f = KbFilter()
    assert f.task_id is None
    assert f.session_id is None
    assert f.category is None


def test_filter_accepts_all_fields_when_provided() -> None:
    """All three dimensions accept explicit string values."""
    f = KbFilter(task_id="t1", session_id="s1", category="cat")
    assert f.task_id == "t1"
    assert f.session_id == "s1"
    assert f.category == "cat"


def test_filter_three_states_when_none_empty_and_value() -> None:
    """Three-state: None (global), "" (public), "val" (isolated).

    Given: three filters differing only in task_id state.
    When: compared.
    Then: each represents a distinct filtering intent.
    """
    global_f = KbFilter(task_id=None)
    public_f = KbFilter(task_id="")
    isolated_f = KbFilter(task_id="task-42")

    assert global_f.task_id is None
    assert public_f.task_id == ""
    assert isolated_f.task_id == "task-42"
    assert global_f != public_f
    assert public_f != isolated_f


def test_filter_frozen_raises_when_mutation() -> None:
    """Frozen model rejects attribute assignment."""
    f = KbFilter(task_id="t1")
    with pytest.raises(ValidationError):
        f.__setattr__("task_id", "t2")


def test_filter_rejects_extra_field_when_unknown() -> None:
    """extra='forbid' rejects undeclared fields."""
    with pytest.raises(ValidationError):
        KbFilter.model_validate({"task_id": "t1", "surprise": "boom"})


# ── KbEntry ───────────────────────────────────────────────────────────


def _full_entry() -> KbEntry:
    return KbEntry(
        entry_id=1001,
        key="deploy-steps",
        value="git pull && npm run build",
        task_id="task-1",
        session_id="sess-1",
        category="ops",
        tags="deploy,ci",
        created_at=1700000000,
        updated_at=1700000001,
    )


def test_entry_defaults_isolation_empty_when_omitted() -> None:
    """Isolation fields default to '' (public/no-scope), not None."""
    e = KbEntry(entry_id=1, key="k", value="v", created_at=0, updated_at=0)
    assert e.task_id == ""
    assert e.session_id == ""
    assert e.category == ""
    assert e.tags == ""


def test_entry_accepts_all_fields_when_full() -> None:
    """Every field can be populated."""
    e = _full_entry()
    assert e.entry_id == 1001
    assert e.key == "deploy-steps"
    assert e.value == "git pull && npm run build"
    assert e.task_id == "task-1"
    assert e.session_id == "sess-1"
    assert e.category == "ops"
    assert e.tags == "deploy,ci"
    assert e.created_at == 1700000000
    assert e.updated_at == 1700000001


def test_entry_frozen_raises_when_mutation() -> None:
    """Frozen model rejects attribute assignment."""
    e = KbEntry(entry_id=1, key="k", value="v", created_at=0, updated_at=0)
    with pytest.raises(ValidationError):
        e.__setattr__("value", "changed")


def test_entry_rejects_extra_field_when_unknown() -> None:
    """extra='forbid' rejects undeclared fields."""
    with pytest.raises(ValidationError):
        KbEntry.model_validate({
            "entry_id": 1, "key": "k", "value": "v",
            "created_at": 0, "updated_at": 0, "bogus": True,
        })


# ── KbUpsertRequest ───────────────────────────────────────────────────


def test_upsert_defaults_empty_when_omitted() -> None:
    """Only key + value are required; isolation fields default to ''."""
    r = KbUpsertRequest(key="k", value="v")
    assert r.key == "k"
    assert r.value == "v"
    assert r.task_id == ""
    assert r.session_id == ""
    assert r.category == ""
    assert r.tags == ""


def test_upsert_accepts_required_only_when_minimal() -> None:
    """Minimal construction with just key + value succeeds."""
    r = KbUpsertRequest(key="deploy", value="steps here")
    assert r.key == "deploy"
    assert r.value == "steps here"


def test_upsert_accepts_all_fields_when_full() -> None:
    """Every field can be populated."""
    r = KbUpsertRequest(
        key="k", value="v",
        task_id="t1", session_id="s1", category="cat", tags="a,b",
    )
    assert r.task_id == "t1"
    assert r.session_id == "s1"
    assert r.category == "cat"
    assert r.tags == "a,b"


def test_upsert_missing_required_raises_when_key_absent() -> None:
    """key is required — omitting it raises ValidationError."""
    with pytest.raises(ValidationError):
        KbUpsertRequest.model_validate({"value": "v"})


def test_upsert_frozen_raises_when_mutation() -> None:
    """Frozen model rejects attribute assignment."""
    r = KbUpsertRequest(key="k", value="v")
    with pytest.raises(ValidationError):
        r.__setattr__("value", "changed")


def test_upsert_rejects_extra_field_when_unknown() -> None:
    """extra='forbid' rejects undeclared fields."""
    with pytest.raises(ValidationError):
        KbUpsertRequest.model_validate({"key": "k", "value": "v", "nope": 1})


# ── KbSearchResult ────────────────────────────────────────────────────


def _sample_entry() -> KbEntry:
    return KbEntry(entry_id=5, key="k", value="v", created_at=1, updated_at=2)


def test_search_result_defaults_score_zero_when_omitted() -> None:
    """score defaults to 0.0 when not provided."""
    r = KbSearchResult(entry=_sample_entry())
    assert r.score == 0.0


def test_search_result_accepts_entry_and_score() -> None:
    """Both entry and score can be set."""
    r = KbSearchResult(entry=_sample_entry(), score=0.95)
    assert r.entry.entry_id == 5
    assert r.score == pytest.approx(0.95)


def test_search_result_entry_must_be_kbentry_when_wrong_type() -> None:
    """entry field validates as KbEntry — a dict is coerced, a non-coercible
    value raises."""
    r = KbSearchResult.model_validate({
        "entry": {
            "entry_id": 1, "key": "k", "value": "v",
            "created_at": 0, "updated_at": 0,
        },
    })
    assert isinstance(r.entry, KbEntry)
    assert r.entry.entry_id == 1


def test_search_result_frozen_raises_when_mutation() -> None:
    """Frozen model rejects attribute assignment."""
    r = KbSearchResult(entry=_sample_entry(), score=0.5)
    with pytest.raises(ValidationError):
        r.__setattr__("score", 0.99)


def test_search_result_rejects_extra_field_when_unknown() -> None:
    """extra='forbid' rejects undeclared fields."""
    with pytest.raises(ValidationError):
        KbSearchResult.model_validate({"entry": _sample_entry(), "extra": "x"})


# ── Serialization round-trip (rule 13) ────────────────────────────────


def test_entry_round_trips_through_model_dump_when_serialized() -> None:
    """model_dump / model_validate round-trip preserves all fields."""
    original = _full_entry()
    restored = KbEntry.model_validate(original.model_dump())
    assert restored == original


def test_search_result_round_trips_through_model_dump_when_serialized() -> None:
    """Nested Model survives dump/validate round-trip."""
    original = KbSearchResult(entry=_sample_entry(), score=0.42)
    restored = KbSearchResult.model_validate(original.model_dump())
    assert restored == original
    assert restored.entry.entry_id == 5


# ── KbAction ──────────────────────────────────────────────────────────


def test_action_has_all_five_members_when_defined() -> None:
    """KbAction declares the five KB operations with correct string values."""
    assert KbAction.SEARCH == "search"
    assert KbAction.GET == "get"
    assert KbAction.SET == "set"
    assert KbAction.DELETE == "delete"
    assert KbAction.LIST == "list"
    assert len(KbAction) == 5


def test_action_lookup_by_value_returns_member_when_valid() -> None:
    """KbAction('search') resolves to KbAction.SEARCH (StrEnum value lookup)."""
    assert KbAction("search") is KbAction.SEARCH
    assert KbAction("delete") is KbAction.DELETE


def test_action_lookup_raises_when_value_invalid() -> None:
    """KbAction('invalid') raises ValueError — closed enum."""
    with pytest.raises(ValueError):
        KbAction("invalid")


# ── KbControlRequest ──────────────────────────────────────────────────


def test_control_request_accepts_all_fields_when_full() -> None:
    """All fields can be populated."""
    f = KbFilter(task_id="t1", category="ops")
    r = KbControlRequest(
        action=KbAction.SET,
        query_or_key="deploy-key",
        value="git pull",
        filter=f,
        limit=50,
    )
    assert r.action is KbAction.SET
    assert r.query_or_key == "deploy-key"
    assert r.value == "git pull"
    assert r.filter == f
    assert r.limit == 50


def test_control_request_defaults_when_action_only() -> None:
    """Only action is required; query_or_key/value default to None, filter to
    KbFilter(), limit to 20."""
    r = KbControlRequest(action=KbAction.LIST)
    assert r.action is KbAction.LIST
    assert r.query_or_key is None
    assert r.value is None
    assert r.filter == KbFilter()
    assert r.limit == 20


def test_control_request_frozen_raises_when_mutation() -> None:
    """Frozen model rejects attribute assignment."""
    r = KbControlRequest(action=KbAction.LIST)
    with pytest.raises(ValidationError):
        r.__setattr__("limit", 100)


def test_control_request_rejects_extra_field_when_unknown() -> None:
    """extra='forbid' rejects undeclared fields."""
    with pytest.raises(ValidationError):
        KbControlRequest.model_validate({"action": "list", "bogus": True})


@pytest.mark.parametrize("limit", [0, -1, 101])
def test_control_request_rejects_limit_outside_bounds(limit: int) -> None:
    with pytest.raises(ValidationError):
        KbControlRequest(action=KbAction.SEARCH, limit=limit)


@pytest.mark.parametrize("limit", [1, 100])
def test_control_request_accepts_limit_at_bounds(limit: int) -> None:
    request = KbControlRequest(action=KbAction.SEARCH, limit=limit)

    assert request.limit == limit


def test_control_request_model_dump_json_produces_correct_dict_when_called() -> None:
    """model_dump(mode='json') yields a JSON-serializable dict with the action
    serialized as its string value and filter as a nested dict.

    Given: a KbControlRequest with a populated filter and omitted value.
    When: model_dump(mode='json') is called.
    Then: the dict matches the expected JSON-shape structure exactly.
    """
    f = KbFilter(task_id="t1")
    r = KbControlRequest(
        action=KbAction.SEARCH,
        query_or_key="deploy",
        filter=f,
        limit=10,
    )
    dumped = r.model_dump(mode="json")
    assert dumped == {
        "action": "search",
        "query_or_key": "deploy",
        "value": None,
        "filter": {"task_id": "t1", "session_id": None, "category": None},
        "limit": 10,
    }
