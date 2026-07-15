from unittest.mock import AsyncMock

import pytest
from bot.service._external_coding_wiring import _OpenCodeFallbackBackend


@pytest.mark.asyncio
async def test_fallback_close_attempts_every_owned_backend() -> None:
    # Given
    backend = _OpenCodeFallbackBackend()
    sse_close = AsyncMock(side_effect=RuntimeError("SSE close failed"))
    subprocess_close = AsyncMock()
    backend._sse_backend.close = sse_close
    backend._subprocess_backend.close = subprocess_close

    # When
    with pytest.raises(RuntimeError, match="SSE close failed"):
        await backend.close()

    # Then
    sse_close.assert_awaited_once_with()
    subprocess_close.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_fallback_close_preserves_first_failure_when_both_backends_fail() -> None:
    # Given
    backend = _OpenCodeFallbackBackend()
    sse_close = AsyncMock(side_effect=RuntimeError("SSE close failed"))
    subprocess_close = AsyncMock(side_effect=RuntimeError("subprocess close failed"))
    backend._sse_backend.close = sse_close
    backend._subprocess_backend.close = subprocess_close

    # When
    with pytest.raises(RuntimeError, match="SSE close failed"):
        await backend.close()

    # Then
    sse_close.assert_awaited_once_with()
    subprocess_close.assert_awaited_once_with()
