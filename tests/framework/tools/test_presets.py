import pytest

from modex_agent.tools.presets import (
    make_ast_grep_tools,
)

pytest.importorskip("modex_agent.tools.ast")


def test_ast_grep_factory_returns_both_ast_tools() -> None:
    tools = make_ast_grep_tools()
    names = sorted(t.name for t in tools)
    assert names == ["ast_grep_replace", "ast_grep_search"]
