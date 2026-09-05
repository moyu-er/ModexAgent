"""ToolClassification — the single typed classification-outcome contract.

One ``ApprovalClassifier.classify`` call returns ONE frozen strict value
carrying tier, typed source/actor, guard category, reason, and the audit
outcome as appropriate. No mutable ``last_deny_reason`` side channel, no
tuple returns, no re-classification to recover the deny reason.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from modex_agent.approval.classification import (
    ClassificationSource,
    ToolClassification,
)
from modex_agent.approval.config import AgentApprovalConfig
from modex_agent.approval.constants import ApprovalTier
from modex_agent.approval.runtime import (
    ApprovalClassifier,
    TieredToolApprovalClassifier,
)
from modex_agent.core.agent import AgentContext
from modex_agent.core.message import ToolCall
from modex_agent.core.session_id import SessionInfo
from modex_agent.memory.history import ListMessageHistory
from modex_agent.runtime.approval_decision import (
    ApprovalAuditDecision,
    DecisionActor,
)
from modex_agent.sandbox.verdict import GuardCategory
from modex_agent.tools.manager import InMemoryToolManager

WS = Path("/ws/project")


def _ctx() -> AgentContext:
    return AgentContext(
        system_prompt="test",
        history=ListMessageHistory(),
        tool_manager=InMemoryToolManager(),
        session=SessionInfo.from_str("test.agent"),
    )


def _tiered() -> TieredToolApprovalClassifier:
    return TieredToolApprovalClassifier(
        config=AgentApprovalConfig(enabled=False),
    )


def _guard_classifier():  # type: ignore[no-untyped-def]
    from modex_agent.sandbox.decision import SecurityDecisionService
    from modex_agent.sandbox.security_classifier import SecurityClassifier
    from modex_agent.sandbox.settings import (
        GuardSettings,
        SandboxBackend,
        SandboxPolicy,
        SandboxSettings,
    )

    class _FixedRoot:
        def __init__(self, root: Path) -> None:
            self._root = root

        def current(self) -> Path:
            return self._root

    return SecurityClassifier(
        decision=SecurityDecisionService(
            settings=SandboxSettings.model_validate(
                {
                    "backend": SandboxBackend.HOST,
                    "policy": SandboxPolicy.WORKSPACE_WRITE,
                    "guard": GuardSettings(),
                }
            ),
            workspace_root_provider=_FixedRoot(WS),  # type: ignore[arg-type]
        ),
        inner=_tiered(),
        escalate_enabled=True,
    )


def _deny_rule_call() -> ToolCall:
    return ToolCall(tool_name="bash", arguments={"command": "rm -rf /"}, call_id="c1")


def _boundary_call() -> ToolCall:
    return ToolCall(tool_name="read", arguments={"path": "/etc/passwd"}, call_id="c1")


def _clean_call() -> ToolCall:
    return ToolCall(tool_name="bash", arguments={"command": f"ls {WS}"}, call_id="c1")


class TestValueContract:
    def test_frozen(self) -> None:
        result = ToolClassification(tier=ApprovalTier.NORMAL)
        with pytest.raises(ValidationError):
            result.tier = ApprovalTier.HARDLINE  # type: ignore[misc]

    def test_extra_forbidden(self) -> None:
        with pytest.raises(ValidationError):
            ToolClassification(tier=ApprovalTier.NORMAL, bogus="x")  # type: ignore[call-arg]

    def test_tier_result_defaults_to_tier_source_without_audit(self) -> None:
        result = ToolClassification.tier_result(ApprovalTier.DANGEROUS)
        assert result.tier is ApprovalTier.DANGEROUS
        assert result.source is ClassificationSource.TIER
        assert result.reason is None
        assert result.guard_category is None
        assert result.audit is None

    def test_audit_fact_rejects_approved(self) -> None:
        from modex_agent.approval.classification import GuardAuditFact

        with pytest.raises(ValidationError):
            GuardAuditFact(
                decision=ApprovalAuditDecision.APPROVED,
                decided_by=DecisionActor.SANDBOX_GUARD,
            )

    def test_audit_fact_requires_guard_source(self) -> None:
        from modex_agent.approval.classification import GuardAuditFact

        with pytest.raises(ValidationError):
            ToolClassification(
                tier=ApprovalTier.NORMAL,
                source=ClassificationSource.TIER,
                audit=GuardAuditFact(
                    decision=ApprovalAuditDecision.DENIED,
                    decided_by=DecisionActor.SANDBOX_GUARD,
                ),
            )


class TestTieredClassifierReturnsClassification:
    def test_normal_call(self) -> None:
        result = _tiered().classify(_clean_call(), _ctx())
        assert isinstance(result, ToolClassification)
        assert result.tier is ApprovalTier.NORMAL
        assert result.source is ClassificationSource.TIER
        assert result.audit is None


class TestSecurityClassifierReturnsClassification:
    def test_hardline_deny_carries_reason_category_and_audit(self) -> None:
        result = _guard_classifier().classify(_deny_rule_call(), _ctx())
        assert result.tier is ApprovalTier.HARDLINE
        assert result.source is ClassificationSource.GUARD
        assert result.guard_category is GuardCategory.DENY_RULE
        assert result.reason is not None
        assert result.audit is not None
        assert result.audit.decision is ApprovalAuditDecision.DENIED
        assert result.audit.decided_by is DecisionActor.SANDBOX_GUARD

    def test_boundary_escalation_is_escalated_never_approved(self) -> None:
        result = _guard_classifier().classify(_boundary_call(), _ctx())
        assert result.tier is ApprovalTier.DANGEROUS
        assert result.source is ClassificationSource.GUARD
        assert result.guard_category is GuardCategory.BOUNDARY
        assert result.audit is not None
        assert result.audit.decision is ApprovalAuditDecision.ESCALATED
        assert result.audit.decided_by is DecisionActor.SANDBOX_GUARD

    def test_clean_falls_back_to_inner_classification(self) -> None:
        result = _guard_classifier().classify(_clean_call(), _ctx())
        assert result.tier is ApprovalTier.NORMAL
        assert result.source is ClassificationSource.TIER
        assert result.audit is None

    def test_no_side_channel_properties(self) -> None:
        """The ABC and implementers carry no mutable reason side channel."""
        assert not hasattr(ApprovalClassifier, "last_deny_reason")
        assert not hasattr(ApprovalClassifier, "last_escalation_reason")
        classifier = _guard_classifier()
        assert not hasattr(classifier, "last_deny_reason")
        assert not hasattr(classifier, "last_escalation_reason")

    def test_no_cross_call_reason_bleed(self) -> None:
        classifier = _guard_classifier()
        denied = classifier.classify(_deny_rule_call(), _ctx())
        clean = classifier.classify(_clean_call(), _ctx())
        assert denied.audit is not None
        assert clean.audit is None
        assert clean.reason is None

    def test_deny_message_builder_shapes_reason(self) -> None:
        from modex_agent.sandbox.decision import SecurityDecisionService
        from modex_agent.sandbox.security_classifier import SecurityClassifier
        from modex_agent.sandbox.settings import (
            GuardSettings,
            SandboxBackend,
            SandboxPolicy,
            SandboxSettings,
        )

        class _FixedRoot:
            def __init__(self, root: Path) -> None:
                self._root = root

            def current(self) -> Path:
                return self._root

        classifier = SecurityClassifier(
            decision=SecurityDecisionService(
                settings=SandboxSettings.model_validate(
                    {
                        "backend": SandboxBackend.HOST,
                        "policy": SandboxPolicy.WORKSPACE_WRITE,
                        "guard": GuardSettings(),
                    }
                ),
                workspace_root_provider=_FixedRoot(WS),  # type: ignore[arg-type]
            ),
            inner=_tiered(),
            escalate_enabled=False,
            deny_message_builder=lambda reason, tool, _raw: f"[{tool}] {reason}",
        )
        result = classifier.classify(_boundary_call(), _ctx())
        assert result.tier is ApprovalTier.HARDLINE
        assert result.audit is not None
        assert result.audit.decision is ApprovalAuditDecision.DENIED
        assert result.reason is not None
        assert result.reason.startswith("[read]")
