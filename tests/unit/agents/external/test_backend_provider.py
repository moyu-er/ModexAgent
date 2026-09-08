"""Unit tests for :class:`PoolScopedBackendProvider`.

The ``CachingBackendProvider`` and ``BackendFactory`` were deleted after
the ``OpenCodeServerManager`` singleton refactor made them vestigial —
``OpenCodeServerBackend.close()`` is a no-op, and the singleton manages
process lifecycle. Both main-agent and subagent external paths now use
:class:`PoolScopedBackendProvider`.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest

from modex_agent.agents.external.agent import StreamingProviderBackend
from modex_agent.agents.external.backend_provider import (
    BackendProvider,
    PoolScopedBackendProvider,
    TurnContext,
)
from modex_agent.agents.external.types import (
    BackendResult,
    BackendStatus,
    Emission,
    ExecOptions,
)
from modex_agent.core.agent import ProviderKind


class _CloseCountingBackend(StreamingProviderBackend):
    def __init__(self, *, label: str = "") -> None:
        self._label = label
        self.close_calls = 0
        self._closed = False

    @property
    def label(self) -> str:
        return self._label

    async def execute_streaming(
        self,
        opts: ExecOptions,
        env: dict[str, str],
        on_emission: Callable[[Emission], Awaitable[None]],
    ) -> BackendResult:
        return BackendResult(status=BackendStatus.COMPLETED)

    async def close(self) -> None:
        self.close_calls += 1
        self._closed = True


def _ctx(workdir: Path | None = None) -> TurnContext:
    return TurnContext(provider_kind=ProviderKind.OPENCODE, workdir=workdir)


class TestPoolScopedBackendProviderIdentity:
    @pytest.mark.asyncio
    async def test_acquire_returns_same_backend_every_time(self, tmp_path: Path) -> None:
        backend = _CloseCountingBackend()
        provider = PoolScopedBackendProvider(backend)
        ctx = _ctx(tmp_path)

        first = await provider.acquire("sid-1", ctx)
        second = await provider.acquire("sid-2", ctx)

        assert first is backend
        assert second is backend

    @pytest.mark.asyncio
    async def test_release_is_no_op_regardless_of_turn_failed(self) -> None:
        backend = _CloseCountingBackend()
        provider = PoolScopedBackendProvider(backend)

        await provider.release(backend, turn_failed=False)
        await provider.release(backend, turn_failed=True)

        assert backend.close_calls == 0

    @pytest.mark.asyncio
    async def test_close_all_calls_backend_close_exactly_once(self) -> None:
        backend = _CloseCountingBackend()
        provider = PoolScopedBackendProvider(backend)

        await provider.close_all()

        assert backend.close_calls == 1

    @pytest.mark.asyncio
    async def test_close_all_propagates_close_failure(self) -> None:
        failure = RuntimeError("close failed")

        class _CloseFailingBackend(StreamingProviderBackend):
            async def execute_streaming(
                self,
                opts: ExecOptions,
                env: dict[str, str],
                on_emission: Callable[[Emission], Awaitable[None]],
            ) -> BackendResult:
                return BackendResult(status=BackendStatus.COMPLETED)

            async def close(self) -> None:
                raise failure

        provider = PoolScopedBackendProvider(_CloseFailingBackend())

        with pytest.raises(RuntimeError, match="close failed"):
            await provider.close_all()

    @pytest.mark.asyncio
    async def test_is_a_backend_provider(self) -> None:
        backend = _CloseCountingBackend()
        provider = PoolScopedBackendProvider(backend)
        assert isinstance(provider, BackendProvider)
