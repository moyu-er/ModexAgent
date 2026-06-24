from __future__ import annotations

from modex_agent.core.graph.engine import GraphEngine


def test_engine_uses_injected_extractor() -> None:
    calls: list[str] = []

    class _Graph:
        entry_node = "END"
        result_extractor = staticmethod(lambda ctx: calls.append("extracted") or "RESULT")

    result = GraphEngine(_Graph()).build_result(ctx=object())  # type: ignore[arg-type]
    assert result == "RESULT"
    assert calls == ["extracted"]


def test_engine_returns_none_without_extractor() -> None:
    class _Graph:
        entry_node = "END"
        result_extractor = None

    # extractor unset (None) -> build_result returns None
    assert GraphEngine(_Graph()).build_result(ctx=object()) is None  # type: ignore[arg-type]
