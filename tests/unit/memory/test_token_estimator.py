"""Tests for the TokenEstimator seam and CharTokenEstimator default."""
from __future__ import annotations

from typing import Any

import pytest

from modex_agent.core.message import ChatMessage
from modex_agent.memory.token_estimator import (
    CharTokenEstimator,
    TokenEstimator,
    message_payload,
)


def test_message_payload_collects_all_fields() -> None:
    msg: dict[str, Any] = {
        "role": "tool",
        "content": "result-body",
        "name": "search",
        "tool_call_id": "call_1",
        "tool_calls": [{"id": "call_1", "function": {"name": "f", "arguments": "{}"}}],
    }
    payload = message_payload(msg)
    assert "result-body" in payload
    assert "search" in payload
    assert "call_1" in payload
    assert "function" in payload  # tool_calls JSON


def test_char_estimator_text_is_ascii_div4() -> None:
    est = CharTokenEstimator()
    # 8 ASCII chars -> 2 tokens (floor of 8/4)
    assert est.estimate_text("abcdefgh") == 2


def test_char_estimator_text_is_cjk_per_char() -> None:
    est = CharTokenEstimator()
    # 3 CJK chars -> 3 tokens (1 per non-ascii char)
    assert est.estimate_text("你好吗") == 3


def test_char_estimator_message_includes_overhead() -> None:
    est = CharTokenEstimator()
    msg = ChatMessage(role="user", content="abcdefgh")  # 2 content tokens
    # estimate_message = estimate_text(payload) + 4 overhead
    assert est.estimate_message(msg) == 2 + 4


def test_char_estimator_summed_messages() -> None:
    est = CharTokenEstimator()
    msgs = [
        ChatMessage(role="user", content="abcdefgh"),    # 2 + 4 = 6
        ChatMessage(role="assistant", content="abcdefgh"),  # 6
    ]
    assert est.estimate_messages(msgs) == 12


def test_token_estimator_is_abstract() -> None:
    with pytest.raises(TypeError):
        TokenEstimator()  # type: ignore[abstract]


def test_chatmessage_token_count_round_trips() -> None:
    msg = ChatMessage(role="user", content="hi", token_count=42)
    d = msg.to_dict()
    assert d.get("token_count") == 42
    restored = ChatMessage.from_dict(d)
    assert restored.token_count == 42


def test_chatmessage_token_count_default_none_omitted() -> None:
    d = ChatMessage(role="user", content="hi").to_dict()
    assert "token_count" not in d  # exclude_none drops it


@pytest.mark.asyncio
async def test_scoped_message_history_stamps_token_count(tmp_path) -> None:
    from modex_agent.core.scope import MemoryContext
    from modex_agent.memory.default_system import ScopedMessageHistory
    from modex_agent.memory.layers.factory import MemoryLayerFactory
    from modex_agent.memory.registry import DefaultMemoryStoreRegistry

    registry = DefaultMemoryStoreRegistry(tmp_path)
    layer_set = MemoryLayerFactory.single_user(registry=registry)
    ctx = MemoryContext(session_id="s1", user_id="u1")
    hist = ScopedMessageHistory(manager=layer_set.session, context=ctx)
    await hist.append({"role": "user", "content": "abcdefgh"})  # 2 content tokens + 4 overhead = 6
    msgs = await hist.to_list()
    assert msgs[-1].token_count == 6


@pytest.mark.asyncio
async def test_cleanup_uses_injected_estimator_not_default(tmp_path) -> None:
    """ScopedMessageHistory must forward its estimator to cleanup_session so
    trigger/boundary share the same estimator as stamping. Regression for the
    divergence bug where cleanup silently fell back to CharTokenEstimator."""
    from modex_agent.core.scope import MemoryContext
    from modex_agent.memory.default_system import ScopedMessageHistory
    from modex_agent.memory.layers.factory import MemoryLayerFactory
    from modex_agent.memory.registry import DefaultMemoryStoreRegistry

    class HugeEstimator(TokenEstimator):
        """Every message is enormous -> cleanup always triggers and prunes hard."""

        def estimate_text(self, text: str) -> int:
            return 1_000_000

    registry = DefaultMemoryStoreRegistry(tmp_path)
    layer_set = MemoryLayerFactory.single_user(registry=registry)
    ctx = MemoryContext(session_id="s1", user_id="u1")
    hist = ScopedMessageHistory(
        manager=layer_set.session,
        context=ctx,
        cleanup_config={"max_context_tokens": 100, "max_token_ratio": 0.85, "keep_ratio": 0.3},
        token_estimator=HugeEstimator(),
    )
    for i in range(3):
        await hist.append({"role": "user", "content": f"msg-{i}"})

    msgs = await hist.to_list()
    # HugeEstimator -> each message ~1M tokens -> trigger fires every append,
    # boundary keeps only the floor-of-1 tail. Had cleanup fallen back to the
    # default CharTokenEstimator, 3 short messages (~15 tokens) would NOT cross
    # the 85-token line and all 3 would survive.
    assert len(msgs) == 1
