"""Unit tests for :func:`render_system_prompt`."""

from __future__ import annotations

from modex_agent.agents.external_coding.system_prompt import render_system_prompt


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
        out = render_system_prompt(
            [("zeta", "z desc"), ("alpha", "a desc")]
        )
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
