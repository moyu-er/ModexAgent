"""FanInInputAdapter resilience: a channel that fails to connect must not
abort startup of the other channels. Regression for the Telegram-behind-firewall
crash where one IM adapter's ``start()`` raising took down the whole bot."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest
from bot.adapters.fan_in import FanInInputAdapter

from modex_agent.core.types import InputMessage
from modex_agent.pipeline.adapters import InputAdapter


class _StubSource(InputAdapter):
    """Minimal InputAdapter stub for fan-in isolation tests."""

    def __init__(self, name: str, *, fail_start: bool = False, fail_stop: bool = False) -> None:
        super().__init__()
        self._name = name
        self._fail_start = fail_start
        self._fail_stop = fail_stop
        self.started = False
        self.stopped = False

    @property
    def name(self) -> str:
        return self._name

    async def start(self) -> None:
        if self._fail_start:
            raise ConnectionError(f"{self._name}: cannot reach platform")
        self.started = True

    async def stop(self) -> None:
        self.stopped = True
        if self._fail_stop:
            raise RuntimeError(f"{self._name}: stop blew up")

    async def receive(self) -> AsyncIterator[InputMessage]:
        # Empty async generator — pump task parks harmlessly.
        if False:
            yield  # pragma: no cover


class _ProducingSource(_StubSource):
    """A healthy source that yields exactly one message, then parks."""

    def __init__(self, name: str, msg: InputMessage) -> None:
        super().__init__(name)
        self._msg = msg

    async def receive(self) -> AsyncIterator[InputMessage]:
        yield self._msg


@pytest.mark.asyncio
async def test_failing_source_does_not_abort_start_of_healthy_source() -> None:
    fan_in = FanInInputAdapter()
    failing = _StubSource("failing_im", fail_start=True)
    healthy = _ProducingSource(
        "healthy_im",
        InputMessage(content="hi", session=None, channel="healthy_im"),  # type: ignore[arg-type]
    )
    fan_in.add_source(failing)
    fan_in.add_source(healthy)

    # Must NOT raise — the failing channel is disabled, the healthy one starts.
    await fan_in.start()

    assert healthy.started is True
    assert failing.started is False
    # only the healthy source gets a pump task
    assert len(fan_in._pump_tasks) == 1  # noqa: SLF001

    # the healthy channel's messages still reach the merged stream
    msg = await asyncio.wait_for(fan_in.receive().__anext__(), timeout=1.0)
    assert msg.content == "hi"

    await fan_in.stop()


@pytest.mark.asyncio
async def test_failing_stop_does_not_abort_stop_of_other_sources() -> None:
    fan_in = FanInInputAdapter()
    bad_stop = _StubSource("bad_stop", fail_stop=True)
    good = _StubSource("good")
    fan_in.add_source(bad_stop)
    fan_in.add_source(good)
    await fan_in.start()

    # Must NOT raise — one source's stop() failure is isolated.
    await fan_in.stop()

    assert good.stopped is True
