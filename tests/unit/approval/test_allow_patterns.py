"""bash allow_patterns gray-zone noise reduction (unified-security Ticket 04).

``ToolApprovalConfig.allow_patterns`` is a de-noising whitelist only:
a hit classifies the tool call NORMAL (no card); it NEVER overrides a
deny verdict — the guard layer wrapping the classifier evaluates deny
rules first and always wins. Patterns are full-command regexes
(``re.fullmatch``): an allowed prefix cannot approve an appended shell
operation. Default ``[]`` keeps behavior identical.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from modex_agent.approval.config import AgentApprovalConfig, ToolApprovalConfig
from modex_agent.approval.constants import ApprovalTier
from modex_agent.approval.runtime import (
    TieredToolApprovalClassifier,
    command_matches_allow_patterns,
)
from modex_agent.core.agent import AgentContext
from modex_agent.core.message import ToolCall
from modex_agent.core.session_id import SessionInfo
from modex_agent.ioc.configs.approval import ApprovalConfig, ToolApprovalEntry
from modex_agent.ioc.factories.approval import build_approval_runtime
from modex_agent.memory.history import ListMessageHistory
from modex_agent.tools.manager import InMemoryToolManager
from modex_agent.tools.workspace_scoped import WorkspaceRootProvider

WS = Path("/ws/project")


class _FixedRoot(WorkspaceRootProvider):
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


def _classifier(
    tools: dict[str, ToolApprovalConfig],
) -> TieredToolApprovalClassifier:
    return TieredToolApprovalClassifier(
        config=AgentApprovalConfig(enabled=True, tools=tools),
    )


def _bash(command: str) -> ToolCall:
    return ToolCall(tool_name="bash", arguments={"command": command}, call_id="c1")


# ---------------------------------------------------------------------------
# Pure matcher
# ---------------------------------------------------------------------------


class TestCommandMatchesAllowPatterns:
    def test_exact_command_hits(self) -> None:
        assert command_matches_allow_patterns("git status", ["git status", "pytest .*"])

    def test_prefix_pattern_covers_arguments(self) -> None:
        assert command_matches_allow_patterns("git commit -m x", ["git .*"])

    def test_non_matching_command_misses(self) -> None:
        assert not command_matches_allow_patterns("cargo build", ["git .*", "pytest .*"])

    def test_appended_shell_command_cannot_ride_a_prefix(self) -> None:
        # Full-command semantics: an allowed prefix must not approve an
        # appended shell operation. (``git .*`` WOULD match — an explicit
        # glob is the declarer's own precision choice; the exact pattern
        # must stay exact.)
        assert not command_matches_allow_patterns("git status; rm -rf /", ["git status"])
        assert not command_matches_allow_patterns("git status && cat /etc/passwd", ["git status"])

    def test_case_insensitive(self) -> None:
        assert command_matches_allow_patterns("GIT status", ["git status"])

    def test_empty_patterns_never_match(self) -> None:
        assert not command_matches_allow_patterns("git status", [])


# ---------------------------------------------------------------------------
# Classifier semantics
# ---------------------------------------------------------------------------


class TestClassifierAllowPatterns:
    def test_allow_hit_classifies_normal_even_without_allowed_paths(self) -> None:
        # bash never has path arguments — the ticket's motivating case:
        # allowed_paths=[] would gate every command DANGEROUS; the allow
        # hit de-noises to NORMAL (no card).
        classifier = _classifier(
            {"bash": ToolApprovalConfig(allowed_paths=[], allow_patterns=["git .*", "pytest .*"])}
        )
        assert classifier.classify(_bash("git status"), _ctx()).tier is ApprovalTier.NORMAL
        assert classifier.classify(_bash("pytest -q"), _ctx()).tier is ApprovalTier.NORMAL

    def test_allow_miss_still_dangerous(self) -> None:
        classifier = _classifier(
            {"bash": ToolApprovalConfig(allowed_paths=[], allow_patterns=["git .*"])}
        )
        assert classifier.classify(_bash("cargo build"), _ctx()).tier is ApprovalTier.DANGEROUS

    def test_default_empty_patterns_keep_current_behavior(self) -> None:
        # Default [] must be bit-identical to pre-T04: a gated bash
        # command classifies DANGEROUS through the existing path.
        classifier = _classifier({"bash": ToolApprovalConfig(allowed_paths=[])})
        assert classifier.classify(_bash("git status"), _ctx()).tier is ApprovalTier.DANGEROUS

    def test_allow_patterns_do_not_affect_path_tools(self) -> None:
        # The matcher only reads the ``command`` argument; a write tool
        # without one never hits the whitelist (path rules still apply).
        from modex_agent.interceptor.builtin.tool_approval import ArgumentMatcher

        classifier = TieredToolApprovalClassifier(
            config=AgentApprovalConfig(
                enabled=True,
                tools={"write": ToolApprovalConfig(allowed_paths=["./*"], allow_patterns=[".*"])},
            ),
            argument_matcher=ArgumentMatcher(root_provider=_FixedRoot(WS)),
        )
        tc = ToolCall(tool_name="write", arguments={"path": "/etc/passwd"}, call_id="c1")
        assert classifier.classify(tc, _ctx()).tier is ApprovalTier.DANGEROUS


# ---------------------------------------------------------------------------
# Deny wins — guard verdicts are evaluated BEFORE the inner whitelist
# ---------------------------------------------------------------------------


class TestDenyAlwaysWins:
    def _composite_rt(self, patterns: list[str]):
        from modex_agent.sandbox.settings import (
            GuardSettings,
            SandboxBackend,
            SandboxPolicy,
            SandboxSettings,
        )

        return build_approval_runtime(
            ApprovalConfig(
                enabled=True,
                tools={"bash": ToolApprovalEntry(allowed_paths=[], allow_patterns=patterns)},
            ),
            root_provider=_FixedRoot(WS),
            sandbox=SandboxSettings.model_validate(
                {
                    "backend": SandboxBackend.HOST,
                    "policy": SandboxPolicy.WORKSPACE_WRITE,
                    "guard": GuardSettings(),
                }
            ),
        )

    def test_deny_hit_beats_matching_allow_pattern(self) -> None:
        # "rm -rf /" matches BOTH the blanket allow pattern and the guard's
        # deny rule — the deny verdict (HARDLINE) must win: the whitelist
        # only de-noises, it never bypasses a deny.
        rt = self._composite_rt(["rm.*"])
        assert rt is not None
        assert rt.classifier.classify(_bash("rm -rf /"), _ctx()).tier is ApprovalTier.HARDLINE

    def test_allow_hit_through_composite_is_normal_when_clean(self) -> None:
        # A clean command hitting the allow pattern stays NORMAL through
        # the full composite (guard CLEAN -> inner allow hit).
        rt = self._composite_rt(["git .*"])
        assert rt is not None
        assert rt.classifier.classify(_bash("git status"), _ctx()).tier is ApprovalTier.NORMAL


# ---------------------------------------------------------------------------
# Scope declaration chain: YAML entry -> ToolApprovalEntry -> runtime
# ---------------------------------------------------------------------------


class TestDeclarationChain:
    def test_yaml_entry_parses_allow_patterns(self) -> None:
        cfg = ApprovalConfig.model_validate(
            {
                "enabled": True,
                "tools": {"bash": {"allowed_paths": [], "allow_patterns": ["git .*"]}},
            }
        )
        assert cfg.tools["bash"].allow_patterns == ["git .*"]

    def test_invalid_regex_rejected_at_parse(self) -> None:
        # Config boundary validates every pattern compiles — a broken
        # regex must fail fast, not surface as a runtime error on the
        # approval path.
        with pytest.raises(ValidationError, match="allow_patterns"):
            ToolApprovalEntry(allow_patterns=["git *[("])

    def test_invalid_regex_rejected_from_raw_yaml(self) -> None:
        with pytest.raises(ValidationError):
            ApprovalConfig.model_validate(
                {
                    "enabled": True,
                    "tools": {"bash": {"allowed_paths": [], "allow_patterns": ["([bad"]}},
                }
            )

    def test_framework_config_rejects_invalid_regex(self) -> None:
        with pytest.raises(ValidationError, match="allow_patterns"):
            ToolApprovalConfig(allow_patterns=["*[bad"])

    def test_valid_patterns_parse(self) -> None:
        entry = ToolApprovalEntry(allow_patterns=["git status", r"pytest -q \d+"])
        assert entry.allow_patterns == ["git status", r"pytest -q \d+"]
        cfg = ToolApprovalConfig(allow_patterns=["git status", r"pytest -q \d+"])
        assert cfg.allow_patterns == ["git status", r"pytest -q \d+"]

    def test_factory_forwards_allow_patterns_to_classifier(self) -> None:
        rt = build_approval_runtime(
            ApprovalConfig(
                enabled=True,
                tools={"bash": ToolApprovalEntry(allowed_paths=[], allow_patterns=["git .*"])},
            ),
            project_root=WS,
        )
        assert rt is not None
        assert isinstance(rt.classifier, TieredToolApprovalClassifier)
        assert rt.classifier.config.tools["bash"].allow_patterns == ["git .*"]
        assert rt.classifier.classify(_bash("git status"), _ctx()).tier is ApprovalTier.NORMAL
        assert rt.classifier.classify(_bash("make"), _ctx()).tier is ApprovalTier.DANGEROUS

    def test_backward_compatible_entry_without_allow_patterns(self) -> None:
        # Existing YAML without the new field parses with the default [].
        cfg = ApprovalConfig.model_validate(
            {"enabled": True, "tools": {"bash": {"allowed_paths": []}}}
        )
        assert cfg.tools["bash"].allow_patterns == []
