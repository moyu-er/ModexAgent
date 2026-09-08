"""SecurityClassifier — guard verdicts riding the approval tier channel.

Coverage per unified-security Ticket 02: the PRD §判定→动作固定映射表
(deny rule → HARDLINE; approvable categories boundary/SSRF →
escalate-aware; clean → inner tiered classifier), the escalate=False
DENIED semantics (HARDLINE + recorded deny reason), CLEAN falling back
to the inner ``TieredToolApprovalClassifier`` unchanged, and the
assembly-time containment check (approval ``allowed_paths`` ⊆ sandbox
envelope).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from modex_agent.approval.config import AgentApprovalConfig, ToolApprovalConfig
from modex_agent.approval.constants import ApprovalTier
from modex_agent.approval.runtime import TieredToolApprovalClassifier
from modex_agent.core.agent import AgentContext
from modex_agent.core.message import ToolCall
from modex_agent.core.session_id import SessionInfo
from modex_agent.ioc.configs.approval import ApprovalConfig, ToolApprovalEntry
from modex_agent.ioc.factories.approval import build_approval_runtime
from modex_agent.memory.history import ListMessageHistory
from modex_agent.sandbox.security_classifier import (
    SecurityClassifier,
    validate_approval_envelope,
)
from modex_agent.sandbox.settings import (
    GuardSettings,
    SandboxBackend,
    SandboxSettings,
    WriteSurface,
)
from modex_agent.tools.manager import InMemoryToolManager
from modex_agent.tools.workspace_scoped import WorkspaceRootProvider

WS = Path("/ws/project")


class _FixedRoot(WorkspaceRootProvider):
    """WorkspaceRootProvider returning a fixed root."""

    def __init__(self, root: Path) -> None:
        self._root = root

    def current(self) -> Path:
        return self._root


def _ctx() -> AgentContext:
    return AgentContext(
        system_prompt="test",
        history=ListMessageHistory(),
        tool_manager=InMemoryToolManager(),
        session=SessionInfo.from_str("test.agent"),
    )


def _settings(
    write_surface: WriteSurface = WriteSurface.WORKSPACE,
    guard: GuardSettings | None = None,
    writable_roots: list[Path] | None = None,
) -> SandboxSettings:
    exclusive: dict[str, object] = {"write_surface": write_surface.value}
    if writable_roots is not None:
        exclusive["writable_roots"] = writable_roots
    kwargs: dict[str, object] = {
        "backend": SandboxBackend.HOST,
        "exclusive": exclusive,
    }
    if guard is not None:
        kwargs["guard"] = guard
    return SandboxSettings.model_validate(kwargs)


def _inner(
    enabled: bool = True,
    tools: dict[str, ToolApprovalConfig] | None = None,
) -> TieredToolApprovalClassifier:
    return TieredToolApprovalClassifier(
        config=AgentApprovalConfig(enabled=enabled, tools=tools or {}),
    )


def _classifier(
    *,
    escalate: bool = True,
    inner: TieredToolApprovalClassifier | None = None,
    settings: SandboxSettings | None = None,
) -> SecurityClassifier:
    from modex_agent.sandbox.decision import SecurityDecisionService

    return SecurityClassifier(
        decision=SecurityDecisionService(
            settings=settings or _settings(),
            workspace_root_provider=_FixedRoot(WS),
        ),
        inner=inner if inner is not None else _inner(),
        escalate_enabled=escalate,
    )


# ---------------------------------------------------------------------------
# Mapping matrix — PRD §判定→动作固定映射表
# ---------------------------------------------------------------------------


class TestHardlineMapping:
    """DENY_RULE → HARDLINE regardless of escalate."""

    @pytest.mark.parametrize("escalate", [True, False])
    def test_deny_rule_command_maps_to_hardline(self, escalate: bool) -> None:
        # Deny rules are opt-in (deprecated usage) — enable to verify the
        # mapping itself.
        classifier = _classifier(
            escalate=escalate,
            settings=_settings(guard=GuardSettings(deny_rules=True)),
        )
        tc = ToolCall(tool_name="bash", arguments={"command": "rm -rf /"}, call_id="c1")
        assert classifier.classify(tc, _ctx()).tier is ApprovalTier.HARDLINE

    @pytest.mark.parametrize("escalate", [True, False])
    def test_ssrf_url_escalates_like_boundary(self, escalate: bool) -> None:
        classifier = _classifier(escalate=escalate)
        tc = ToolCall(
            tool_name="web_reader",
            arguments={"url": "http://169.254.169.254/latest/meta-data"},
            call_id="c1",
        )
        assert classifier.classify(tc, _ctx()).tier is ApprovalTier.DANGEROUS if escalate else ApprovalTier.HARDLINE

    def test_ssrf_bash_command_escalates_like_boundary(self) -> None:
        # bash curl rides the same evaluate_command seam → SSRF — the
        # approvable gray zone, one escalation branch with BOUNDARY.
        classifier = _classifier(escalate=True)
        tc = ToolCall(
            tool_name="bash",
            arguments={"command": "curl http://169.254.169.254/latest/meta-data"},
            call_id="c1",
        )
        assert classifier.classify(tc, _ctx()).tier is ApprovalTier.DANGEROUS

    @pytest.mark.parametrize("escalate", [True, False])
    def test_read_only_write_tool_maps_to_hardline(self, escalate: bool) -> None:
        classifier = _classifier(
            escalate=escalate, settings=_settings(write_surface=WriteSurface.NONE)
        )
        tc = ToolCall(
            tool_name="write", arguments={"path": "src/new.py"}, call_id="c1"
        )
        assert classifier.classify(tc, _ctx()).tier is ApprovalTier.HARDLINE

    def test_hardline_deny_reason_carries_verdict_reason(self) -> None:
        # The reason channel: classify returns HARDLINE for deny-rule with
        # the verdict reason recorded for the deny-side surface.
        classifier = _classifier(settings=_settings(guard=GuardSettings(deny_rules=True)))
        tc = ToolCall(tool_name="bash", arguments={"command": "rm -rf /"}, call_id="c1")
        result = classifier.classify(tc, _ctx())
        recorded = result.deny_reason
        assert recorded is not None
        assert "denied" in recorded.lower() or "rm" in recorded.lower()


class TestBoundaryMapping:
    """BOUNDARY → DANGEROUS (escalate) / HARDLINE + reason (no escalation)."""

    def test_boundary_file_path_escalates_to_dangerous(self) -> None:
        classifier = _classifier(escalate=True)
        tc = ToolCall(tool_name="write", arguments={"path": "/etc/hosts"}, call_id="c1")
        assert classifier.classify(tc, _ctx()).tier is ApprovalTier.DANGEROUS

    def test_boundary_command_escalates_to_dangerous(self) -> None:
        classifier = _classifier(escalate=True)
        # Write-capable command: a provable read would be CLEAN.
        tc = ToolCall(
            tool_name="bash", arguments={"command": "touch /etc/passwd"}, call_id="c1"
        )
        assert classifier.classify(tc, _ctx()).tier is ApprovalTier.DANGEROUS

    def test_boundary_no_escalation_denies_as_hardline_with_reason(self) -> None:
        classifier = _classifier(escalate=False)
        tc = ToolCall(tool_name="write", arguments={"path": "/etc/hosts"}, call_id="c1")
        result = classifier.classify(tc, _ctx())
        assert result.tier is ApprovalTier.HARDLINE
        # DENIED semantics ride the reason channel: ToolNode maps HARDLINE
        # to ApprovalDecision.DENIED; the boundary fact is preserved.
        recorded = result.deny_reason
        assert recorded is not None
        assert "/etc/hosts" in recorded

    def test_boundary_reason_does_not_bleed_into_clean_call(self) -> None:
        classifier = _classifier(escalate=False)
        denied = ToolCall(tool_name="write", arguments={"path": "/etc/hosts"}, call_id="c1")
        denied_result = classifier.classify(denied, _ctx())
        clean = ToolCall(tool_name="read", arguments={"path": "src/main.py"}, call_id="c2")
        result = classifier.classify(clean, _ctx())
        assert result.tier is ApprovalTier.NORMAL  # inner: not in config → NORMAL
        assert result.deny_reason is None
        assert denied_result.deny_reason is not None


class TestCleanFallback:
    """CLEAN → the inner TieredToolApprovalClassifier, behavior unchanged."""

    def test_clean_inside_allowed_paths_is_normal(self) -> None:
        inner = _inner(
            tools={"write": ToolApprovalConfig(allowed_paths=["./*"])}
        )
        # The inner matcher is optional in the dataclass; give it the real
        # ArgumentMatcher through the factory path for pattern resolution.
        from modex_agent.interceptor.builtin.tool_approval import ArgumentMatcher

        inner.argument_matcher = ArgumentMatcher(root_provider=_FixedRoot(WS))
        classifier = _classifier(inner=inner)
        tc = ToolCall(tool_name="write", arguments={"path": "src/a.py"}, call_id="c1")
        assert classifier.classify(tc, _ctx()).tier is ApprovalTier.NORMAL

    def test_clean_outside_allowed_paths_is_dangerous(self) -> None:
        inner = _inner(
            tools={"write": ToolApprovalConfig(allowed_paths=["./*"])}
        )
        from modex_agent.interceptor.builtin.tool_approval import ArgumentMatcher

        inner.argument_matcher = ArgumentMatcher(root_provider=_FixedRoot(WS))
        classifier = _classifier(inner=inner)
        tc = ToolCall(tool_name="write", arguments={"path": "src/a.py"}, call_id="c1")
        assert classifier.classify(tc, _ctx()).tier is ApprovalTier.NORMAL
        # Empty-allowed tool gates as DANGEROUS through the inner path.
        inner_empty = _inner(tools={"bash": ToolApprovalConfig(allowed_paths=[])})
        classifier_empty = _classifier(inner=inner_empty)
        tc_cmd = ToolCall(tool_name="bash", arguments={"command": "ls"}, call_id="c2")
        assert classifier_empty.classify(tc_cmd, _ctx()).tier is ApprovalTier.DANGEROUS

    def test_clean_unguarded_tool_passes_through(self) -> None:
        # Tools outside the guard matrix (D/E/F classes) fall to the inner
        # classifier verbatim.
        classifier = _classifier(inner=_inner(enabled=False))
        tc = ToolCall(tool_name="todo_write", arguments={"items": []}, call_id="c1")
        assert classifier.classify(tc, _ctx()).tier is ApprovalTier.NORMAL

    def test_clean_fallback_with_approval_disabled_inner(self) -> None:
        # escalate=False + inner disabled → the guard-only composite: gray
        # commands (no deny hit) stay NORMAL (P1 semantics: approval-off
        # gray-zone passes; the interceptor still judges at execution).
        classifier = _classifier(escalate=False, inner=_inner(enabled=False))
        tc = ToolCall(tool_name="bash", arguments={"command": f"ls {WS}"}, call_id="c1")
        assert classifier.classify(tc, _ctx()).tier is ApprovalTier.NORMAL


# ---------------------------------------------------------------------------
# Assembly — build_approval_runtime(sandbox=...)
# ---------------------------------------------------------------------------


class TestBuildApprovalRuntimeComposite:
    def _cfg(self) -> ApprovalConfig:
        return ApprovalConfig(
            enabled=True,
            tools={"write": ToolApprovalEntry(allowed_paths=["./*"])},
        )

    def test_no_sandbox_returns_plain_tiered_classifier(self) -> None:
        rt = build_approval_runtime(self._cfg(), project_root=Path(str(WS)))
        assert rt is not None
        assert isinstance(rt.classifier, TieredToolApprovalClassifier)

    def test_default_backend_returns_plain_tiered_classifier(self) -> None:
        # DEFAULT tier: the composite must NOT exist — runtime is the plain
        # approval classifier, identical to today.
        rt = build_approval_runtime(
            self._cfg(),
            project_root=Path(str(WS)),
            sandbox=_settings().model_copy(
                update={"backend": SandboxBackend.DEFAULT}
            ),
        )
        assert rt is not None
        assert type(rt.classifier) is TieredToolApprovalClassifier

    def test_sandbox_on_wraps_composite_with_inner(self) -> None:
        rt = build_approval_runtime(
            self._cfg(),
            root_provider=_FixedRoot(WS),
            sandbox=_settings(),
        )
        assert rt is not None
        assert isinstance(rt.classifier, SecurityClassifier)
        assert isinstance(rt.classifier.inner, TieredToolApprovalClassifier)

    def test_sandbox_on_without_approval_builds_guard_only_composite(self) -> None:
        # 审批未接线的 sandbox-on 部署: composite 仍挂 (escalate=False 的
        # guard-only), 保证灰区拒 — build returns an ApprovalRuntime even
        # when the approval config is a no-op.
        rt = build_approval_runtime(
            None,
            root_provider=_FixedRoot(WS),
            sandbox=_settings(),
        )
        assert rt is not None
        assert isinstance(rt.classifier, SecurityClassifier)
        assert rt.classifier.escalate_enabled is False

    def test_sandbox_on_composite_classifies_deny_rule_hardline(self) -> None:
        rt = build_approval_runtime(
            self._cfg(),
            root_provider=_FixedRoot(WS),
            sandbox=_settings(guard=GuardSettings(deny_rules=True)),
        )
        assert rt is not None
        tc = ToolCall(tool_name="bash", arguments={"command": "rm -rf /"}, call_id="c1")
        assert rt.classifier.classify(tc, _ctx()).tier is ApprovalTier.HARDLINE


# ---------------------------------------------------------------------------
# Containment — allowed_paths ⊆ sandbox envelope (assembly fail-fast)
# ---------------------------------------------------------------------------


class TestContainmentValidation:
    def test_inside_workspace_passes(self) -> None:
        validate_approval_envelope(
            ApprovalConfig(
                enabled=True,
                tools={"write": ToolApprovalEntry(allowed_paths=["./*"])},
            ),
            settings=_settings(),
            root_provider=_FixedRoot(WS),
        )

    def test_writable_root_extension_passes(self) -> None:
        validate_approval_envelope(
            ApprovalConfig(
                enabled=True,
                tools={
                    "write": ToolApprovalEntry(
                        allowed_paths=[str(WS / "src" / "**")]
                    )
                },
            ),
            settings=_settings(writable_roots=[WS / "vendor"]),
            root_provider=_FixedRoot(WS),
        )

    def test_outside_envelope_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="envelope"):
            validate_approval_envelope(
                ApprovalConfig(
                    enabled=True,
                    tools={
                        "write": ToolApprovalEntry(
                            allowed_paths=["/etc/**"]
                        )
                    },
                ),
                settings=_settings(),
                root_provider=_FixedRoot(WS),
            )

    def test_star_wildcard_passes(self) -> None:
        # "*" / "**" mean "everywhere" — the envelope check cannot judge a
        # universal pattern; it stays legal (documented semantics) and the
        # runtime guard remains the second gate.
        validate_approval_envelope(
            ApprovalConfig(
                enabled=True,
                tools={"write": ToolApprovalEntry(allowed_paths=["*"])},
            ),
            settings=_settings(),
            root_provider=_FixedRoot(WS),
        )

    def test_danger_full_access_skips_check(self) -> None:
        # No envelope is declared under DANGER_FULL_ACCESS — nothing to
        # validate against.
        validate_approval_envelope(
            ApprovalConfig(
                enabled=True,
                tools={"write": ToolApprovalEntry(allowed_paths=["/etc/**"])},
            ),
            settings=_settings(write_surface=WriteSurface.FULL),
            root_provider=_FixedRoot(WS),
        )

    def test_disabled_approval_config_skips_check(self) -> None:
        validate_approval_envelope(
            ApprovalConfig(enabled=False),
            settings=_settings(),
            root_provider=_FixedRoot(WS),
        )

    def test_factory_assembly_raises_on_outside_allowed_paths(self) -> None:
        with pytest.raises(ValueError, match="envelope"):
            build_approval_runtime(
                ApprovalConfig(
                    enabled=True,
                    tools={"write": ToolApprovalEntry(allowed_paths=["/etc/**"])},
                ),
                root_provider=_FixedRoot(WS),
                sandbox=_settings(),
            )

    def test_factory_assembly_no_raise_inside(self) -> None:
        rt = build_approval_runtime(
            ApprovalConfig(
                enabled=True,
                tools={"write": ToolApprovalEntry(allowed_paths=["./*"])},
            ),
            root_provider=_FixedRoot(WS),
            sandbox=_settings(),
        )
        assert rt is not None
        assert isinstance(rt.classifier, SecurityClassifier)


# ---------------------------------------------------------------------------
# Ticket 05b — deny_message_builder + guard_only_runtime convergence
# ---------------------------------------------------------------------------


class TestDenyMessageBuilder:
    """The builder re-shapes the deny reason into deployment copy."""

    def test_builder_output_replaces_recorded_reason(self) -> None:
        from modex_agent.sandbox.decision import SecurityDecisionService

        classifier = SecurityClassifier(
            decision=SecurityDecisionService(
                settings=_settings(), workspace_root_provider=_FixedRoot(WS)
            ),
            inner=_inner(enabled=False),
            escalate_enabled=False,
            deny_message_builder=lambda reason, tool, _raw: (
                f"[boundary:{tool}] {reason} — escalate in the main session"
            ),
        )
        tc = ToolCall(tool_name="write", arguments={"path": "/etc/hosts"}, call_id="c1")
        result = classifier.classify(tc, _ctx())
        assert result.tier is ApprovalTier.HARDLINE
        recorded = result.deny_reason
        assert recorded is not None
        assert recorded.startswith("[boundary:write]")
        assert recorded.endswith("escalate in the main session")

    def test_no_builder_keeps_raw_reason(self) -> None:
        classifier = _classifier(escalate=False)
        tc = ToolCall(tool_name="write", arguments={"path": "/etc/hosts"}, call_id="c1")
        result = classifier.classify(tc, _ctx())
        recorded = result.deny_reason
        assert recorded is not None
        assert "[boundary:" not in recorded
        assert "/etc/hosts" in recorded


class TestGuardOnlyRuntime:
    """The converge helper builds the escalate-off composite every caller shares."""

    def _rt(self, builder=None):  # type: ignore[no-untyped-def]
        from modex_agent.sandbox.security_classifier import guard_only_runtime

        if builder is None:
            return guard_only_runtime(
                settings=_settings(), root_provider=_FixedRoot(WS)
            )
        return guard_only_runtime(
            settings=_settings(),
            root_provider=_FixedRoot(WS),
            deny_message_builder=builder,
        )

    def test_shape_is_security_classifier_with_disabled_inner(self) -> None:
        rt = self._rt()
        classifier = rt.classifier
        assert isinstance(classifier, SecurityClassifier)
        assert classifier.escalate_enabled is False
        assert isinstance(classifier.inner, TieredToolApprovalClassifier)
        assert classifier.inner.config.enabled is False

    def test_guard_verdicts_deny_without_card_channel(self) -> None:
        rt = self._rt()
        tc = ToolCall(tool_name="write", arguments={"path": "/etc/hosts"}, call_id="c1")
        assert rt.classifier.classify(tc, _ctx()).tier is ApprovalTier.HARDLINE

    def test_clean_calls_stay_normal(self) -> None:
        rt = self._rt()
        tc = ToolCall(tool_name="read", arguments={"path": "src/a.py"}, call_id="c1")
        assert rt.classifier.classify(tc, _ctx()).tier is ApprovalTier.NORMAL

    def test_builder_threads_through_to_recorded_reason(self) -> None:
        rt = self._rt(
            lambda reason, tool, _raw: f"[delegation:{tool}] {reason}"
        )
        tc = ToolCall(tool_name="write", arguments={"path": "/etc/hosts"}, call_id="c1")
        result = rt.classifier.classify(tc, _ctx())
        assert result.tier is ApprovalTier.HARDLINE
        classifier = rt.classifier
        assert isinstance(classifier, SecurityClassifier)
        recorded = result.deny_reason
        assert recorded is not None
        assert recorded.startswith("[delegation:write]")

    def test_factory_guard_only_path_and_helper_build_same_shape(self) -> None:
        # Convergence: build_approval_runtime's no-approval branch uses the
        # same helper — the classifier type and escalate bit match.
        factory_rt = build_approval_runtime(
            None, root_provider=_FixedRoot(WS), sandbox=_settings()
        )
        assert factory_rt is not None
        helper_rt = self._rt()
        factory_classifier = factory_rt.classifier
        helper_classifier = helper_rt.classifier
        assert isinstance(factory_classifier, SecurityClassifier)
        assert isinstance(helper_classifier, SecurityClassifier)
        assert type(factory_classifier) is type(helper_classifier)
        assert (
            factory_classifier.escalate_enabled == helper_classifier.escalate_enabled
            is False
        )
