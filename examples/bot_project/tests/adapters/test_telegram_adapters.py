from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from bot.adapters.telegram import TelegramInputAdapter, TelegramOutputAdapter

from modex_agent.core.types import OutputMessage


def test_input_adapter_name() -> None:
    inp = TelegramInputAdapter(token="t", allow_from=["*"], proxy=None)
    assert inp.name == "telegram"


def test_input_adapter_allow_from_filter() -> None:
    restricted = TelegramInputAdapter(token="t", allow_from=["123"], proxy=None)
    assert restricted.is_allowed("123") is True
    assert restricted.is_allowed("999") is False
    wild = TelegramInputAdapter(token="t", allow_from=["*"], proxy=None)
    assert wild.is_allowed("anyone") is True


@pytest.mark.asyncio
async def test_handle_text_message_rejects_disallowed_sender() -> None:
    inp = TelegramInputAdapter(token="t", allow_from=["123"], proxy=None)
    # disallowed sender must not reach the pipeline / queue
    await inp.handle_text_message(chat_id="42", text="hello", sender_id="999")
    assert inp._queue.empty()  # noqa: SLF001


@pytest.mark.asyncio
async def test_receive_yields_input_message_after_put() -> None:
    from modex_agent.core.session_id import SessionInfo
    from modex_agent.core.types import InputMessage

    inp = TelegramInputAdapter(token="t", allow_from=["*"], proxy=None)
    await inp.start()
    session = SessionInfo.from_str("42.main", default_agent_name="main")
    # S8 EnqueueStage path: a fully-built InputMessage is pushed via
    # put_input_message and drained by receive().
    inp.put_input_message(
        InputMessage(content="hello", session=session, channel="telegram")
    )
    msg = await asyncio.wait_for(inp.receive().__anext__(), timeout=1.0)
    assert msg.content == "hello"
    assert "42" in str(msg.session)
    await inp.stop()


@pytest.mark.asyncio
async def test_handle_text_message_seeds_pipeline_and_surfaces_terminate() -> None:
    from modex_agent.input_pipeline.envelope import UserInputEnvelope
    from modex_agent.input_pipeline.stage import Terminate

    inp = TelegramInputAdapter(token="t", allow_from=["*"], proxy=None)

    captured: dict[str, object] = {}

    async def fake_handle(seed: UserInputEnvelope, ctx: object) -> Terminate:
        captured["external_id"] = seed.external_id
        captured["content"] = seed.content
        captured["channel"] = seed.channel
        return Terminate(reason="pool_switch", response={"message": "switched pool"})

    pipeline = MagicMock()
    pipeline.handle = fake_handle  # type: ignore[method-assign]
    out = MagicMock()
    out.send = AsyncMock()
    inp.configure_input_pipeline(pipeline, MagicMock(), out)

    await inp.handle_text_message(chat_id="42", text="hi", sender_id="42")

    # seed is built from the chat id (the session prefix / external_id)
    assert captured == {"external_id": "42", "content": "hi", "channel": "telegram"}
    # Terminate response surfaced directly to the owning chat
    assert out.send.await_count == 1
    sent_msg, sent_sid = out.send.call_args.args
    assert sent_sid == "42"
    assert sent_msg.content == "switched pool"


@pytest.mark.asyncio
async def test_start_is_noop_without_hook() -> None:
    inp = TelegramInputAdapter(token="t", allow_from=["*"], proxy=None)
    # no lifecycle hook injected -> start()/stop() are safe no-ops
    await inp.start()
    assert inp._start_hook is None  # noqa: SLF001
    assert inp._stop_hook is None  # noqa: SLF001
    await inp.stop()


@pytest.mark.asyncio
async def test_output_send_text_chunks_and_posts() -> None:
    bot = MagicMock()
    bot.send_message = AsyncMock()
    out = TelegramOutputAdapter(bot=bot)
    await out._send_text("a" * 6000, chat_id="42")  # noqa: SLF001  > 4096 -> must chunk into >=2 calls
    assert bot.send_message.await_count >= 2
    # each chunk must respect the 4096 limit and target the resolved chat
    for call in bot.send_message.await_args_list:
        assert call.kwargs["chat_id"] == "42"
        assert len(call.kwargs["text"]) <= 4096


@pytest.mark.asyncio
async def test_output_send_routes_chat_from_session_id() -> None:
    bot = MagicMock()
    bot.send_message = AsyncMock()
    out = TelegramOutputAdapter(bot=bot)
    msg = OutputMessage(content="hi")
    await out.send(msg, session_id="4242.main")
    assert bot.send_message.await_count == 1
    assert bot.send_message.await_args.kwargs["chat_id"] == "4242"


@pytest.mark.asyncio
async def test_output_send_text_html_fallback_on_error() -> None:
    bot = MagicMock()

    async def _fail(*args: object, **kwargs: object) -> None:
        raise RuntimeError("boom")

    bot.send_message = AsyncMock(side_effect=_fail)
    out = TelegramOutputAdapter(bot=bot)
    # must not raise even when send_message fails (plain-text fallback attempt)
    await out._send_text("hi **there**", chat_id="42")
    # attempted at least once; failures swallowed
    assert bot.send_message.await_count >= 1


def test_output_adapter_name() -> None:
    bot = MagicMock()
    out = TelegramOutputAdapter(bot=bot)
    assert out.name == "telegram"
