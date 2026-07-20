"""Tests for Pydantic migration of ReAct state types (ADR-0033 D14).

Verifies that the 5 migrated types (``ApprovalTransaction`` /
``ApprovalRequestState`` / ``ToolBatchState`` / ``ToolCallState`` /
``ToolArguments``) behave correctly as Pydantic ``BaseModel`` subclasses:

- Fields are accessible with the same names and types as before.
- Mutable types (NOT frozen) allow field reassignment and dict mutation
  required by the approval state machine.
- ``ToolArguments`` is frozen — field reassignment raises
  ``ValidationError``.
- ``model_dump()`` / ``model_validate()`` round-trip correctly for
  JSON-serializable fields.
- ``ToolCallState`` accepts ``arbitrary_types_allowed`` for ``ToolResult``.
- Channel codec registrations round-trip via ``register_codec``.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest
from pydantic import BaseModel, ValidationError

import modex_agent.agents.react.codec  # noqa: F401 — registers channel codecs
from modex_agent.approval.constants import ApprovalDecision, ApprovalStatus, ApprovalTier
from modex_agent.runtime.enums import (
    ApprovalSubjectType,
    ToolBatchStatus,
    ToolCallStatus,
)
from modex_agent.runtime.models import (
    ApprovalRequestState,
    ApprovalTransaction,
    ToolArguments,
    ToolBatchState,
    ToolCallState,
)


def _make_request(call_id: str = "call-1", tool_name: str = "read_file") -> ApprovalRequestState:
    return ApprovalRequestState(
        request_id="r1",
        approval_id="ap-1",
        tool_call_id=call_id,
        tool_name=tool_name,
        arguments=ToolArguments(values={"path": "a.txt"}),
        tier=ApprovalTier.DANGEROUS,
        iteration=1,
    )


def _make_transaction() -> ApprovalTransaction:
    return ApprovalTransaction(
        approval_id="ap-1",
        turn_id="t1",
        subject_type=ApprovalSubjectType.TOOL_BATCH,
        subject_ids=["batch-1"],
        requests=[_make_request("call-1"), _make_request("call-2")],
    )


def _make_tool_call(call_id: str = "call-1") -> ToolCallState:
    return ToolCallState(
        call_id=call_id,
        tool_name="read_file",
        arguments=ToolArguments(values={"path": "a.txt"}),
    )


class TestToolArgumentsMigration:
    def test_is_pydantic_model(self) -> None:
        assert issubclass(ToolArguments, BaseModel)

    def test_field_access(self) -> None:
        args = ToolArguments(values={"path": "notes.md", "limit": 3})
        assert args.values["path"] == "notes.md"
        assert args.values["limit"] == 3

    def test_frozen_prevents_reassignment(self) -> None:
        args = ToolArguments(values={"path": "a.txt"})
        with pytest.raises(ValidationError):
            args.values = {"path": "b.txt"}

    def test_model_dump_round_trip(self) -> None:
        args = ToolArguments(values={"path": "a.txt", "count": 3, "nested": {"k": "v"}})
        dumped = args.model_dump(mode="json")
        restored = ToolArguments.model_validate(dumped)
        assert restored.values["path"] == "a.txt"
        assert restored.values["count"] == 3
        nested = restored.values["nested"]
        assert isinstance(nested, dict)
        assert nested["k"] == "v"


class TestApprovalRequestStateMigration:
    def test_is_pydantic_model(self) -> None:
        assert issubclass(ApprovalRequestState, BaseModel)

    def test_fields(self) -> None:
        req = _make_request()
        assert req.request_id == "r1"
        assert req.approval_id == "ap-1"
        assert req.tool_call_id == "call-1"
        assert req.tool_name == "read_file"
        assert isinstance(req.arguments, ToolArguments)
        assert req.tier == ApprovalTier.DANGEROUS
        assert req.iteration == 1
        assert req.created_at > 0

    def test_mutable_allows_field_update(self) -> None:
        req = _make_request()
        req.tool_name = "write_file"
        assert req.tool_name == "write_file"

    def test_model_dump_round_trip(self) -> None:
        req = _make_request()
        dumped = req.model_dump(mode="json")
        restored = ApprovalRequestState.model_validate(dumped)
        assert restored.request_id == req.request_id
        assert restored.tool_call_id == req.tool_call_id
        assert restored.tier == req.tier
        assert restored.arguments.values["path"] == "a.txt"


class TestApprovalTransactionMigration:
    def test_is_pydantic_model(self) -> None:
        assert issubclass(ApprovalTransaction, BaseModel)

    def test_default_status_is_pending(self) -> None:
        tx = _make_transaction()
        assert tx.status == ApprovalStatus.PENDING
        assert tx.decisions == {}
        assert tx.deny_reason is None

    def test_apply_decision_mutates_decisions_dict(self) -> None:
        tx = _make_transaction()
        tx.apply_decision("call-1", ApprovalDecision.ALLOWED)
        assert tx.decisions["call-1"] == ApprovalDecision.ALLOWED
        assert tx.status == ApprovalStatus.PARTIAL

    def test_deny_preempts_unresolved(self) -> None:
        tx = _make_transaction()
        tx.apply_decision("call-1", ApprovalDecision.DENIED, reason="not allowed")
        assert tx.decisions["call-1"] == ApprovalDecision.DENIED
        assert tx.decisions["call-2"] == ApprovalDecision.PREEMPTED
        assert tx.status == ApprovalStatus.DENIED
        assert tx.deny_reason == "not allowed"

    def test_all_allowed_marks_approved(self) -> None:
        tx = _make_transaction()
        tx.apply_decision("call-1", ApprovalDecision.ALLOWED)
        tx.apply_decision("call-2", ApprovalDecision.ALLOWED)
        assert tx.status == ApprovalStatus.APPROVED

    def test_normalize_batch_decisions_can_rewrite_allowed(self) -> None:
        decisions = [ApprovalDecision.ALLOWED, ApprovalDecision.DENIED]
        has_denial = any(
            d in (ApprovalDecision.DENIED, ApprovalDecision.PREEMPTED) for d in decisions
        )
        assert has_denial
        normalized = [
            ApprovalDecision.PREEMPTED if d == ApprovalDecision.ALLOWED else d for d in decisions
        ]
        assert normalized == [ApprovalDecision.PREEMPTED, ApprovalDecision.DENIED]

    def test_mutable_status_reassignment(self) -> None:
        tx = _make_transaction()
        tx.status = ApprovalStatus.APPROVED
        assert tx.status == ApprovalStatus.APPROVED

    def test_mutable_deny_reason_reassignment(self) -> None:
        tx = _make_transaction()
        tx.deny_reason = "user rejected"
        assert tx.deny_reason == "user rejected"

    def test_every_tool_decided_property(self) -> None:
        tx = _make_transaction()
        assert tx.every_tool_decided is False
        tx.apply_decision("call-1", ApprovalDecision.ALLOWED)
        assert tx.every_tool_decided is False
        tx.apply_decision("call-2", ApprovalDecision.ALLOWED)
        assert tx.every_tool_decided is True

    def test_model_dump_round_trip(self) -> None:
        tx = _make_transaction()
        tx.apply_decision("call-1", ApprovalDecision.ALLOWED)
        tx.apply_decision("call-2", ApprovalDecision.DENIED, reason="no")
        dumped = tx.model_dump(mode="json")
        restored = ApprovalTransaction.model_validate(dumped)
        assert restored.approval_id == tx.approval_id
        assert restored.status == ApprovalStatus.DENIED
        assert restored.decisions["call-2"] == ApprovalDecision.DENIED
        assert restored.decisions["call-1"] == ApprovalDecision.PREEMPTED
        assert restored.deny_reason == "no"
        assert len(restored.requests) == 2


class TestToolCallStateMigration:
    def test_is_pydantic_model(self) -> None:
        assert issubclass(ToolCallState, BaseModel)

    def test_default_status_is_pending(self) -> None:
        call = _make_tool_call()
        assert call.status == ToolCallStatus.PENDING
        assert call.decision is None
        assert call.result is None

    def test_mutable_decision_transition(self) -> None:
        call = _make_tool_call()
        call.decision = ApprovalDecision.ALLOWED
        assert call.decision == ApprovalDecision.ALLOWED
        call.decision = ApprovalDecision.PREEMPTED
        assert call.decision == ApprovalDecision.PREEMPTED

    def test_mutable_status_transition(self) -> None:
        call = _make_tool_call()
        call.status = ToolCallStatus.ALLOWED
        assert call.status == ToolCallStatus.ALLOWED
        call.status = ToolCallStatus.COMPLETED
        assert call.status == ToolCallStatus.COMPLETED

    def test_mutable_result_assignment(self) -> None:
        call = _make_tool_call()
        mock_result = MagicMock()
        call.result = mock_result
        assert call.result is mock_result

    def test_arbitrary_types_allowed_for_result(self) -> None:
        from modex_agent.core.tool_manager import ToolResult

        mock_result = MagicMock(spec=ToolResult)
        call = ToolCallState(
            call_id="c1",
            tool_name="bash",
            arguments=ToolArguments(values={}),
            result=mock_result,
        )
        assert call.result is mock_result

    def test_model_dump_round_trip_without_result(self) -> None:
        call = _make_tool_call()
        call.decision = ApprovalDecision.ALLOWED
        call.status = ToolCallStatus.COMPLETED
        dumped = call.model_dump(mode="json")
        restored = ToolCallState.model_validate(dumped)
        assert restored.call_id == call.call_id
        assert restored.tool_name == call.tool_name
        assert restored.decision == ApprovalDecision.ALLOWED
        assert restored.status == ToolCallStatus.COMPLETED
        assert restored.arguments.values["path"] == "a.txt"


class TestToolBatchStateMigration:
    def test_is_pydantic_model(self) -> None:
        assert issubclass(ToolBatchState, BaseModel)

    def test_default_status_is_created(self) -> None:
        batch = ToolBatchState(
            batch_id="b1",
            iteration=1,
            calls=[_make_tool_call()],
        )
        assert batch.status == ToolBatchStatus.CREATED
        assert batch.approval_id is None
        assert batch.operation_id is None

    def test_mutable_status_transition(self) -> None:
        batch = ToolBatchState(
            batch_id="b1",
            iteration=1,
            calls=[_make_tool_call()],
        )
        batch.status = ToolBatchStatus.SUSPENDED
        assert batch.status == ToolBatchStatus.SUSPENDED
        batch.status = ToolBatchStatus.COMPLETED
        assert batch.status == ToolBatchStatus.COMPLETED

    def test_mutable_operation_id_assignment(self) -> None:
        batch = ToolBatchState(
            batch_id="b1",
            iteration=1,
            calls=[],
        )
        batch.operation_id = "op-1"
        assert batch.operation_id == "op-1"

    def test_model_dump_round_trip(self) -> None:
        batch = ToolBatchState(
            batch_id="b1",
            iteration=2,
            calls=[_make_tool_call("c1"), _make_tool_call("c2")],
            approval_id="ap-1",
            status=ToolBatchStatus.SUSPENDED,
            operation_id="op-1",
        )
        dumped = batch.model_dump(mode="json")
        restored = ToolBatchState.model_validate(dumped)
        assert restored.batch_id == "b1"
        assert restored.iteration == 2
        assert restored.approval_id == "ap-1"
        assert restored.status == ToolBatchStatus.SUSPENDED
        assert restored.operation_id == "op-1"
        assert len(restored.calls) == 2
        assert restored.calls[0].call_id == "c1"


class TestCodecRegistration:
    def test_codec_round_trip_approval_transaction(self) -> None:
        from modex_graph.channel import _find_codec

        tx = _make_transaction()
        tx.apply_decision("call-1", ApprovalDecision.ALLOWED)
        codec = _find_codec(ApprovalTransaction)
        assert codec is not None
        encoded = codec.encode(tx)
        decoded = codec.decode(encoded)
        assert isinstance(decoded, ApprovalTransaction)
        assert decoded.approval_id == tx.approval_id
        assert decoded.decisions["call-1"] == ApprovalDecision.ALLOWED

    def test_codec_round_trip_tool_arguments(self) -> None:
        from modex_graph.channel import _find_codec

        args = ToolArguments(values={"path": "a.txt", "count": 3})
        codec = _find_codec(ToolArguments)
        assert codec is not None
        encoded = codec.encode(args)
        decoded = codec.decode(encoded)
        assert isinstance(decoded, ToolArguments)
        assert decoded.values["path"] == "a.txt"
        assert decoded.values["count"] == 3

    def test_codec_round_trip_tool_batch_state(self) -> None:
        from modex_graph.channel import _find_codec

        batch = ToolBatchState(
            batch_id="b1",
            iteration=1,
            calls=[_make_tool_call()],
        )
        codec = _find_codec(ToolBatchState)
        assert codec is not None
        encoded = codec.encode(batch)
        decoded = codec.decode(encoded)
        assert isinstance(decoded, ToolBatchState)
        assert decoded.batch_id == "b1"
        assert len(decoded.calls) == 1

    def test_codec_round_trip_tool_call_state(self) -> None:
        from modex_graph.channel import _find_codec

        call = _make_tool_call()
        call.decision = ApprovalDecision.DENIED
        call.status = ToolCallStatus.DENIED
        codec = _find_codec(ToolCallState)
        assert codec is not None
        encoded = codec.encode(call)
        decoded = codec.decode(encoded)
        assert isinstance(decoded, ToolCallState)
        assert decoded.call_id == call.call_id
        assert decoded.decision == ApprovalDecision.DENIED

    def test_codec_round_trip_approval_request_state(self) -> None:
        from modex_graph.channel import _find_codec

        req = _make_request()
        codec = _find_codec(ApprovalRequestState)
        assert codec is not None
        encoded = codec.encode(req)
        decoded = codec.decode(encoded)
        assert isinstance(decoded, ApprovalRequestState)
        assert decoded.request_id == req.request_id
        assert decoded.tier == ApprovalTier.DANGEROUS

    def test_codec_module_import_registers_all_five(self) -> None:
        import modex_agent.agents.react.codec as _codec_mod  # noqa: F401
        from modex_graph.channel import _find_codec

        for cls in (
            ApprovalTransaction,
            ApprovalRequestState,
            ToolBatchState,
            ToolCallState,
            ToolArguments,
        ):
            assert _find_codec(cls) is not None, f"no codec registered for {cls.__name__}"


class TestBackwardCompatibility:
    def test_tool_arguments_values_is_mapping(self) -> None:
        args = ToolArguments(values={"path": "a.txt"})
        assert args.values["path"] == "a.txt"
        assert dict(args.values) == {"path": "a.txt"}

    def test_approval_transaction_construction_unchanged(self) -> None:
        tx = ApprovalTransaction(
            approval_id="ap-1",
            turn_id="t1",
            subject_type=ApprovalSubjectType.TOOL_BATCH,
            subject_ids=["b1"],
            requests=[],
        )
        assert tx.approval_id == "ap-1"
        assert tx.status == ApprovalStatus.PENDING

    def test_tool_call_state_construction_with_call_id(self) -> None:
        call = ToolCallState(
            call_id="c1",
            tool_name="bash",
            arguments=ToolArguments(values={"cmd": "ls"}),
        )
        assert call.call_id == "c1"
        assert call.tool_name == "bash"

    def test_created_at_default_is_now(self) -> None:
        before = time.time()
        tx = _make_transaction()
        after = time.time()
        assert before <= tx.created_at <= after
