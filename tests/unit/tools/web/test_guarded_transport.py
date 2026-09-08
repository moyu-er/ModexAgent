"""Seam tests for the guarded transport adaptation layer.

``guarded_transport`` owns the httpcore adaptation: mapping expected
httpcore errors at the transport boundary and the async response stream
with its injected close callable. These tests exercise those seams
directly; the end-to-end stack is covered by ``test_guarded_http.py``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpcore
import httpx
import pytest

from modex_agent.tools.web.guarded_transport import (
    _AsyncResponseStream,
    _raise_mapped,
)


async def _no_close() -> None:
    return None


class TestRaiseMapped:
    """Expected httpcore errors map to their httpx twins; unknown re-raise."""

    def test_known_timeout_maps_to_httpx_twin(self) -> None:
        with pytest.raises(httpx.ReadTimeout) as excinfo:
            _raise_mapped(httpcore.ReadTimeout("read timed out"))
        assert "read timed out" in str(excinfo.value)
        assert isinstance(excinfo.value.__cause__, httpcore.ReadTimeout)

    def test_specific_mapping_wins_over_generic_parent(self) -> None:
        # ConnectTimeout is a TimeoutException subclass; the
        # most-specific-first table must pick the narrower httpx twin.
        with pytest.raises(httpx.ConnectTimeout):
            _raise_mapped(httpcore.ConnectTimeout("connect timed out"))

    def test_unknown_error_reraised_unchanged(self) -> None:
        original = ValueError("unknown failure")
        with pytest.raises(ValueError) as excinfo:
            _raise_mapped(original)
        assert excinfo.value is original


class TestAsyncResponseStream:
    async def test_iterates_body_and_maps_mid_stream_error(self) -> None:
        async def body() -> AsyncIterator[bytes]:
            yield b"ok"
            raise httpcore.ReadError("mid-stream failure")

        stream = _AsyncResponseStream(body(), _no_close)
        received: list[bytes] = []
        with pytest.raises(httpx.ReadError) as excinfo:
            async for chunk in stream:
                received.append(chunk)
        assert received == [b"ok"]
        assert "mid-stream failure" in str(excinfo.value)
        assert isinstance(excinfo.value.__cause__, httpcore.ReadError)

    async def test_aclose_calls_the_injected_callable_only(self) -> None:
        calls: list[str] = []

        async def close() -> None:
            calls.append("closed")

        async def body() -> AsyncIterator[bytes]:
            # aclose must never iterate the body — fail loudly if it does.
            raise AssertionError("aclose must not iterate the body")
            yield b""  # pragma: no cover - makes this an async generator

        stream = _AsyncResponseStream(body(), close)
        await stream.aclose()
        assert calls == ["closed"]
