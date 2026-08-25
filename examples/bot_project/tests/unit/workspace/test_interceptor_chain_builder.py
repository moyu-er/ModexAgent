from __future__ import annotations

from pathlib import Path

from bot.workspace.wiring import build_tool_overflow_interceptor_chain

from modex_agent.control.channel import InMemoryControlChannel
from modex_agent.hook.builtin.control_drain import (
    ControlDrainInterceptor,
    LlmCancelInterceptor,
)
from modex_agent.interceptor.builtin import ToolResultLimitInterceptor
from modex_agent.tools.overflow.local import LocalFileToolOverflowStore


def test_builder_without_channel_has_only_tool_result_limit(tmp_path: Path) -> None:
    store = LocalFileToolOverflowStore(workspace=tmp_path)

    chain = build_tool_overflow_interceptor_chain(store, control_channel=None)

    assert [type(interceptor) for interceptor in chain.interceptors] == [
        ToolResultLimitInterceptor
    ]
    limit = chain.interceptors[0]
    assert limit._max_chars == 50_000


def test_builder_with_channel_has_control_interceptors_in_order(tmp_path: Path) -> None:
    store = LocalFileToolOverflowStore(workspace=tmp_path)

    chain = build_tool_overflow_interceptor_chain(
        store,
        control_channel=InMemoryControlChannel(),
    )

    assert [type(interceptor) for interceptor in chain.interceptors] == [
        ToolResultLimitInterceptor,
        ControlDrainInterceptor,
        LlmCancelInterceptor,
    ]


def test_builder_returns_independent_chains(tmp_path: Path) -> None:
    store = LocalFileToolOverflowStore(workspace=tmp_path)

    first = build_tool_overflow_interceptor_chain(store)
    second = build_tool_overflow_interceptor_chain(store)

    first_limit = first.interceptors[0]
    second_limit = second.interceptors[0]
    assert first is not second
    assert first_limit is not second_limit
    assert first_limit.handler is not second_limit.handler
    assert first_limit.handler._cleaner is not second_limit.handler._cleaner
