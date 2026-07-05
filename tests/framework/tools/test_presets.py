from modex_agent.tools.presets import ToolSupplement, get_supplement_tools


def test_ast_grep_supplement_returns_both_ast_tools():
    tools = get_supplement_tools([ToolSupplement.AST_GREP])
    names = sorted(t.name for t in tools)
    assert names == ["ast_grep_replace", "ast_grep_search"]


def test_supplements_dedup_and_empty():
    assert get_supplement_tools([]) == []
    t1 = get_supplement_tools([ToolSupplement.AST_GREP, ToolSupplement.AST_GREP])
    t2 = get_supplement_tools([ToolSupplement.AST_GREP])
    assert sorted(x.name for x in t1) == sorted(x.name for x in t2)
