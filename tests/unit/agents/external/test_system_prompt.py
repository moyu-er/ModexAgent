"""Unit tests for :func:`render_system_prompt`."""

from __future__ import annotations

from modex_agent.agents.external.system_prompt import render_system_prompt
from modex_agent.core.agent import AgentCommKind

_TARGETS = [("helper", "a helper"), ("reviewer", "code reviewer")]


class TestRenderSystemPrompt:
    def test_includes_modexbot_send_instruction(self) -> None:
        out = render_system_prompt([("helper", "a helper")])
        assert "modexctl send --to <name> --content <text>" in out

    def test_states_stdout_not_delivered(self) -> None:
        out = render_system_prompt([])
        assert "NOT delivered" in out

    def test_empty_targets_omits_table(self) -> None:
        out = render_system_prompt([])
        assert "Routable targets" not in out
        assert "|------|" not in out

    def test_targets_table_lists_each_target(self) -> None:
        out = render_system_prompt(
            [
                ("helper", "a helper agent"),
                ("reviewer", "code reviewer"),
            ]
        )
        assert "| helper | a helper agent |" in out
        assert "| reviewer | code reviewer |" in out

    def test_targets_order_preserved(self) -> None:
        out = render_system_prompt([("zeta", "z desc"), ("alpha", "a desc")])
        zeta_idx = out.index("zeta")
        alpha_idx = out.index("alpha")
        assert zeta_idx < alpha_idx

    def test_returns_plain_string(self) -> None:
        out = render_system_prompt([("x", "y")])
        assert isinstance(out, str)

    def test_no_trailing_newline(self) -> None:
        out = render_system_prompt([("x", "y")])
        assert not out.endswith("\n")

    def test_mentions_modexctl_agents(self) -> None:
        out = render_system_prompt([("helper", "a helper")])
        assert "modexctl agents" in out

    def test_does_not_mention_send_to_agent(self) -> None:
        out = render_system_prompt([("helper", "a helper")])
        assert "send_to_agent" not in out
        assert "task" not in out


class TestRenderSystemPromptCommKind:

    def test_normal_contains_modexctl_send(self) -> None:
        assert "modexctl send" in render_system_prompt(_TARGETS, AgentCommKind.NORMAL)

    def test_normal_no_only_way(self) -> None:
        assert "ONLY way" not in render_system_prompt(_TARGETS, AgentCommKind.NORMAL)

    def test_subagent_contains_forwarded_automatically(self) -> None:
        assert "forwarded to your caller automatically" in render_system_prompt(
            _TARGETS, AgentCommKind.SUBAGENT
        )

    def test_subagent_contains_deliverable(self) -> None:
        assert "deliverable" in render_system_prompt(_TARGETS, AgentCommKind.SUBAGENT)

    def test_subagent_no_only_way(self) -> None:
        assert "ONLY way" not in render_system_prompt(_TARGETS, AgentCommKind.SUBAGENT)

    def test_both_contain_targets_table(self) -> None:
        normal = render_system_prompt(_TARGETS, AgentCommKind.NORMAL)
        subagent = render_system_prompt(_TARGETS, AgentCommKind.SUBAGENT)
        assert "| helper | a helper |" in normal
        assert "| helper | a helper |" in subagent

    def test_no_arg_equals_normal(self) -> None:
        assert render_system_prompt(_TARGETS) == render_system_prompt(
            _TARGETS, AgentCommKind.NORMAL
        )
