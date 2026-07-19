"""Seam 2 tests — :class:`BackendProvider` implementations.

Covers the T6 acceptance criteria for the per-turn backend borrowing seam:

- :class:`PoolScopedBackendProvider` identity semantics (main-agent path
  carried over from T2 — single backend, no-op release, ``close_all``
  delegates to the wrapped backend).
- :class:`CachingBackendProvider` warm path: per-modex_session_id
  ``OrderedDict`` cache with ``MAX_WARM_BACKENDS`` LRU cap; LRU touch on
  reuse; cap overflow evicts the LRU entry and calls ``evicted.close()``.
- :class:`CachingBackendProvider` stateless path: shared single instance
  per ``provider_kind`` (no LRU, no cap).
- ``acquire``/``release`` pairing: ``release(turn_failed=False)`` is a
  no-op; ``release(turn_failed=True)`` invalidates the cached warm
  backend for that modex_session_id and closes it.
- ``close_all`` closes every cached warm backend and every shared
  stateless backend, then clears both dicts; subsequent ``acquire``
  raises ``RuntimeError``.
- All cache dict access is guarded by an ``asyncio.Lock`` — concurrent
  acquires from different modex sessions both succeed.

Tests use a counting test double (:class:`_CloseCountingBackend`) that
mirrors the real backends' ``_closed`` flag, plus a recording factory
(:class:`_RecordingFactory`) that lets each test declare which
``provider_kind`` values are warm vs stateless.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import cast

import pytest

from modex_agent.agents.external_coding.agent import StreamingProviderBackend
from modex_agent.agents.external_coding.backend_provider import (
    BackendFactory,
    BackendProvider,
    CachingBackendProvider,
    PoolScopedBackendProvider,
    TurnContext,
)
from modex_agent.agents.external_coding.paths import ProviderKind
from modex_agent.agents.external_coding.types import (
    BackendResult,
    BackendStatus,
    Emission,
    ExecOptions,
)

# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class _CloseCountingBackend(StreamingProviderBackend):
    """Backend test double that counts ``close()`` calls and sets ``_closed``.

    Mirrors the ``_closed`` flag pattern of the real backends
    (:class:`OpenCodeServerBackend`, :class:`OpenCodeBackend`,
    :class:`PiBackend`) so the provider's
    ``getattr(backend, '_closed', False)`` health check exercises a
    realistic code path.
    """

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


class _RecordingFactory(BackendFactory):
    """Records ``create()`` calls and reports configurable warm/stateless.

    Each ``create()`` returns a fresh :class:`_CloseCountingBackend`
    labelled with the provider kind, so tests can distinguish instances
    by ``id()`` or by ``label``. Tests pre-load ``warm_kinds`` to declare
    which provider kinds route through the warm path.
    """

    def __init__(self, *, warm_kinds: frozenset[ProviderKind] = frozenset()) -> None:
        self._warm_kinds = warm_kinds
        self.created: list[ProviderKind] = []
        self.created_backends: list[_CloseCountingBackend] = []

    def create(self, provider_kind: ProviderKind) -> StreamingProviderBackend:
        backend = _CloseCountingBackend(label=provider_kind.value)
        self.created.append(provider_kind)
        self.created_backends.append(backend)
        return backend

    def is_warm(self, provider_kind: ProviderKind) -> bool:
        return provider_kind in self._warm_kinds


def _warm_ctx(workdir: Path | None = None) -> TurnContext:
    return TurnContext(provider_kind=ProviderKind.OPENCODE, workdir=workdir)


def _stateless_opencode_ctx(workdir: Path | None = None) -> TurnContext:
    return TurnContext(provider_kind=ProviderKind.OPENCODE, workdir=workdir)


def _stateless_pi_ctx(workdir: Path | None = None) -> TurnContext:
    return TurnContext(provider_kind=ProviderKind.PI, workdir=workdir)


# ---------------------------------------------------------------------------
# PoolScopedBackendProvider identity (T2 carry-over, Seam 2 surface)
# ---------------------------------------------------------------------------


class TestPoolScopedBackendProviderIdentity:
    """Main-agent path — single pool-scoped backend, identity preserved."""

    @pytest.mark.asyncio
    async def test_acquire_returns_same_backend_every_time(self, tmp_path: Path) -> None:
        backend = _CloseCountingBackend()
        provider = PoolScopedBackendProvider(backend)
        ctx = _stateless_pi_ctx(tmp_path)

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


# ---------------------------------------------------------------------------
# CachingBackendProvider — warm LRU path
# ---------------------------------------------------------------------------


class TestCachingBackendProviderWarmLRU:
    """Warm path: per-modex_session_id cache with LRU cap."""

    @pytest.mark.asyncio
    async def test_acquire_creates_distinct_backend_per_session(self) -> None:
        factory = _RecordingFactory(warm_kinds=frozenset({ProviderKind.OPENCODE}))
        provider = CachingBackendProvider(factory)
        ctx = _warm_ctx()

        a = await provider.acquire("session-A", ctx)
        b = await provider.acquire("session-B", ctx)

        assert a is not b
        assert a is factory.created_backends[0]
        assert b is factory.created_backends[1]
        assert factory.created == [ProviderKind.OPENCODE, ProviderKind.OPENCODE]

    @pytest.mark.asyncio
    async def test_acquire_reuses_cached_backend_for_same_session(self) -> None:
        factory = _RecordingFactory(warm_kinds=frozenset({ProviderKind.OPENCODE}))
        provider = CachingBackendProvider(factory)
        ctx = _warm_ctx()

        first = await provider.acquire("session-A", ctx)
        second = await provider.acquire("session-A", ctx)

        assert first is second
        # Factory created exactly one backend — second acquire hit the cache.
        assert len(factory.created_backends) == 1

    @pytest.mark.asyncio
    async def test_acquire_touches_lru_order_on_reuse(self) -> None:
        """Reusing session A after session B was acquired promotes A to MRU.

        With MAX_WARM_BACKENDS=2, the next acquire for session C must
        evict B (the LRU), not A. This proves ``move_to_end`` runs on
        cache hits.
        """
        factory = _RecordingFactory(warm_kinds=frozenset({ProviderKind.OPENCODE}))
        provider = CachingBackendProvider(factory)
        provider.MAX_WARM_BACKENDS = 2  # type: ignore[misc]
        ctx = _warm_ctx()

        a = cast("_CloseCountingBackend", await provider.acquire("session-A", ctx))  # cache: [A]
        b = cast("_CloseCountingBackend", await provider.acquire("session-B", ctx))  # cache: [A, B]
        a_again = await provider.acquire("session-A", ctx)  # cache: [B, A]
        assert a_again is a

        # Acquiring C must evict B (LRU), not A.
        c = await provider.acquire("session-C", ctx)  # cache: [A, C]

        assert c is not a
        assert c is not b
        # B was evicted — its close() was called exactly once.
        assert b.close_calls == 1
        # A survived — never closed.
        assert a.close_calls == 0

    @pytest.mark.asyncio
    async def test_lru_eviction_at_cap_calls_evicted_close(self) -> None:
        """Acquiring the (N+1)-th session evicts the LRU and closes it."""
        factory = _RecordingFactory(warm_kinds=frozenset({ProviderKind.OPENCODE}))
        provider = CachingBackendProvider(factory)
        cap = CachingBackendProvider.MAX_WARM_BACKENDS
        ctx = _warm_ctx()

        # Fill the cache exactly to cap.
        first_backends: list[_CloseCountingBackend] = []
        for i in range(cap):
            backend = await provider.acquire(f"session-{i}", ctx)
            first_backends.append(backend)  # type: ignore[arg-type]

        assert len(factory.created_backends) == cap

        # Acquire one more — the LRU (session-0) must be evicted.
        extra = await provider.acquire("session-overflow", ctx)

        assert extra is not first_backends[0]
        # The evicted LRU backend was closed exactly once.
        assert first_backends[0].close_calls == 1
        # Every other previously-cached backend survived.
        for survivor in first_backends[1:]:
            assert survivor.close_calls == 0
        # Cache size is still capped.
        assert len(provider._warm_backends) == cap

    @pytest.mark.asyncio
    async def test_lru_eviction_pops_lru_not_mru(self) -> None:
        """The MRU entry (most recently acquired) survives an overflow."""
        factory = _RecordingFactory(warm_kinds=frozenset({ProviderKind.OPENCODE}))
        provider = CachingBackendProvider(factory)
        provider.MAX_WARM_BACKENDS = 3  # type: ignore[misc]
        ctx = _warm_ctx()

        a = cast("_CloseCountingBackend", await provider.acquire("A", ctx))
        b = cast("_CloseCountingBackend", await provider.acquire("B", ctx))
        c = cast("_CloseCountingBackend", await provider.acquire("C", ctx))  # cache: [A, B, C]
        await provider.acquire("D", ctx)  # evicts A; cache: [B, C, D]

        assert a.close_calls == 1  # A was LRU, evicted and closed.
        assert b.close_calls == 0
        assert c.close_calls == 0
        # Reacquiring B returns the cached instance, not a new one.
        b_again = await provider.acquire("B", ctx)
        assert b_again is b


# ---------------------------------------------------------------------------
# CachingBackendProvider — stateless sharing path
# ---------------------------------------------------------------------------


class TestCachingBackendProviderStateless:
    """Stateless path: shared single instance per provider_kind, no LRU."""

    @pytest.mark.asyncio
    async def test_stateless_returns_same_instance_per_kind(self) -> None:
        factory = _RecordingFactory(warm_kinds=frozenset())  # everything stateless
        provider = CachingBackendProvider(factory)

        first = await provider.acquire("session-A", _stateless_pi_ctx())
        second = await provider.acquire("session-B", _stateless_pi_ctx())

        assert first is second
        assert len(factory.created_backends) == 1

    @pytest.mark.asyncio
    async def test_stateless_distinct_kinds_get_distinct_instances(self) -> None:
        factory = _RecordingFactory(warm_kinds=frozenset())
        provider = CachingBackendProvider(factory)

        pi = await provider.acquire("session-A", _stateless_pi_ctx())
        oc = await provider.acquire("session-A", _stateless_opencode_ctx())

        assert pi is not oc
        assert len(factory.created_backends) == 2

    @pytest.mark.asyncio
    async def test_stateless_uncapped_no_eviction_at_cap(self) -> None:
        """Stateless backends never count against MAX_WARM_BACKENDS.

        Acquiring many sessions on a stateless kind must not evict
        anything — the same shared instance is returned each time.
        """
        factory = _RecordingFactory(warm_kinds=frozenset())
        provider = CachingBackendProvider(factory)
        provider.MAX_WARM_BACKENDS = 2  # type: ignore[misc]
        ctx = _stateless_pi_ctx()

        first = cast("_CloseCountingBackend", await provider.acquire("session-0", ctx))
        for i in range(1, 20):
            other = await provider.acquire(f"session-{i}", ctx)
            assert other is first

        # Only one backend was ever created.
        assert len(factory.created_backends) == 1
        # No close() calls — nothing was evicted.
        assert first.close_calls == 0


# ---------------------------------------------------------------------------
# CachingBackendProvider — acquire/release pairing
# ---------------------------------------------------------------------------


class TestCachingBackendProviderRelease:
    """``release`` semantics for warm and stateless paths."""

    @pytest.mark.asyncio
    async def test_release_turn_failed_false_is_no_op_for_warm(self) -> None:
        factory = _RecordingFactory(warm_kinds=frozenset({ProviderKind.OPENCODE}))
        provider = CachingBackendProvider(factory)
        ctx = _warm_ctx()

        backend = cast("_CloseCountingBackend", await provider.acquire("session-A", ctx))
        await provider.release(backend, turn_failed=False)

        # Backend stays in cache; close() was NOT called.
        assert backend.close_calls == 0
        # Reacquire returns the same cached instance.
        again = await provider.acquire("session-A", ctx)
        assert again is backend

    @pytest.mark.asyncio
    async def test_release_turn_failed_true_invalidates_warm_cache(self) -> None:
        factory = _RecordingFactory(warm_kinds=frozenset({ProviderKind.OPENCODE}))
        provider = CachingBackendProvider(factory)
        ctx = _warm_ctx()

        backend = cast("_CloseCountingBackend", await provider.acquire("session-A", ctx))
        await provider.release(backend, turn_failed=True)

        # The failed backend was closed and evicted.
        assert backend.close_calls == 1
        assert "session-A" not in provider._warm_backends
        # Reacquire creates a fresh backend.
        replacement = await provider.acquire("session-A", ctx)
        assert replacement is not backend
        assert len(factory.created_backends) == 2

    @pytest.mark.asyncio
    async def test_release_turn_failed_true_on_stateless_is_no_op(self) -> None:
        """Stateless backends are shared — ``turn_failed`` must not close them."""
        factory = _RecordingFactory(warm_kinds=frozenset())
        provider = CachingBackendProvider(factory)

        backend = cast(
            "_CloseCountingBackend",
            await provider.acquire("session-A", _stateless_pi_ctx()),
        )
        await provider.release(backend, turn_failed=True)

        # Shared instance is NOT closed and NOT removed from the shared dict.
        assert backend.close_calls == 0
        assert ProviderKind.PI in provider._shared_backends
        # Reacquire returns the same shared instance.
        again = await provider.acquire("session-B", _stateless_pi_ctx())
        assert again is backend

    @pytest.mark.asyncio
    async def test_release_unknown_backend_is_no_op(self) -> None:
        """``release`` for a backend the provider never handed out is a no-op."""
        factory = _RecordingFactory(warm_kinds=frozenset({ProviderKind.OPENCODE}))
        provider = CachingBackendProvider(factory)

        stranger = _CloseCountingBackend(label="stranger")
        # Should not raise; should not close.
        await provider.release(stranger, turn_failed=True)

        assert stranger.close_calls == 0


# ---------------------------------------------------------------------------
# CachingBackendProvider — close_all
# ---------------------------------------------------------------------------


class TestCachingBackendProviderCloseAll:
    """``close_all`` cleans up every cached warm + shared stateless backend."""

    @pytest.mark.asyncio
    async def test_close_all_closes_warm_and_stateless_then_clears(self) -> None:
        factory = _RecordingFactory(warm_kinds=frozenset({ProviderKind.OPENCODE}))
        provider = CachingBackendProvider(factory)

        warm_a = cast("_CloseCountingBackend", await provider.acquire("session-A", _warm_ctx()))
        warm_b = cast("_CloseCountingBackend", await provider.acquire("session-B", _warm_ctx()))
        stateless_pi = cast(
            "_CloseCountingBackend",
            await provider.acquire("session-X", _stateless_pi_ctx()),
        )

        await provider.close_all()

        assert warm_a.close_calls == 1
        assert warm_b.close_calls == 1
        assert stateless_pi.close_calls == 1
        assert provider._warm_backends == {}
        assert provider._shared_backends == {}

    @pytest.mark.asyncio
    async def test_close_all_marks_provider_closed_subsequent_acquire_raises(self) -> None:
        factory = _RecordingFactory(warm_kinds=frozenset({ProviderKind.OPENCODE}))
        provider = CachingBackendProvider(factory)

        await provider.acquire("session-A", _warm_ctx())
        await provider.close_all()

        with pytest.raises(RuntimeError, match="closed"):
            await provider.acquire("session-A", _warm_ctx())

    @pytest.mark.asyncio
    async def test_close_all_idempotent_no_double_close(self) -> None:
        factory = _RecordingFactory(warm_kinds=frozenset({ProviderKind.OPENCODE}))
        provider = CachingBackendProvider(factory)

        backend = cast("_CloseCountingBackend", await provider.acquire("session-A", _warm_ctx()))
        await provider.close_all()
        # Second close_all should not double-close (dicts already empty).
        await provider.close_all()

        assert backend.close_calls == 1


# ---------------------------------------------------------------------------
# CachingBackendProvider — concurrency
# ---------------------------------------------------------------------------


class TestCachingBackendProviderConcurrency:
    """All cache dict access is guarded by an ``asyncio.Lock``."""

    @pytest.mark.asyncio
    async def test_concurrent_acquires_for_different_sessions_both_succeed(self) -> None:
        factory = _RecordingFactory(warm_kinds=frozenset({ProviderKind.OPENCODE}))
        provider = CachingBackendProvider(factory)
        ctx = _warm_ctx()

        a, b = await asyncio.gather(
            provider.acquire("session-A", ctx),
            provider.acquire("session-B", ctx),
        )

        assert a is not b
        assert isinstance(a, _CloseCountingBackend)
        assert isinstance(b, _CloseCountingBackend)
        assert len(factory.created_backends) == 2

    @pytest.mark.asyncio
    async def test_concurrent_same_session_acquires_return_same_instance(self) -> None:
        factory = _RecordingFactory(warm_kinds=frozenset({ProviderKind.OPENCODE}))
        provider = CachingBackendProvider(factory)
        ctx = _warm_ctx()

        first, second = await asyncio.gather(
            provider.acquire("session-A", ctx),
            provider.acquire("session-A", ctx),
        )

        # Lock serializes the two acquires; the second one hits the cache
        # populated by the first. Both callers see the same backend.
        assert first is second
        assert len(factory.created_backends) == 1

    @pytest.mark.asyncio
    async def test_concurrent_acquire_with_release_does_not_corrupt_state(self) -> None:
        """``release(turn_failed=True)`` racing with ``acquire`` stays consistent.

        The lock guarantees either: (a) release runs first (cache cleared,
        acquire creates a fresh backend), or (b) acquire runs first (cache
        hit, release then closes that backend and evicts it). Either
        ordering leaves the cache in a legal state.
        """
        factory = _RecordingFactory(warm_kinds=frozenset({ProviderKind.OPENCODE}))
        provider = CachingBackendProvider(factory)
        ctx = _warm_ctx()

        seed = cast("_CloseCountingBackend", await provider.acquire("session-A", ctx))

        # Race a reacquire against a failed release of the seeded backend.
        reacquired, _ = await asyncio.gather(
            provider.acquire("session-A", ctx),
            provider.release(seed, turn_failed=True),
        )

        # Whatever instance ``acquire`` returned, the cache now has at
        # most one entry for session-A and it is consistent with what
        # ``acquire`` returned (either the seed, or a fresh backend
        # placed there after release cleared the slot).
        cached = provider._warm_backends.get("session-A")
        if cached is not None:
            assert cached is reacquired
        # The seed backend was closed exactly once by release.
        assert seed.close_calls == 1


# ---------------------------------------------------------------------------
# BackendFactory ABC shape
# ---------------------------------------------------------------------------


class TestBackendFactoryABC:
    """``BackendFactory`` is an ABC with the two declared abstract methods."""

    def test_cannot_instantiate_directly(self) -> None:
        with pytest.raises(TypeError):
            BackendFactory()  # type: ignore[abstract]

    def test_subclass_must_implement_both_methods(self) -> None:
        class _OnlyCreate(BackendFactory):
            def create(self, provider_kind: ProviderKind) -> StreamingProviderBackend:
                raise NotImplementedError

        with pytest.raises(TypeError):
            _OnlyCreate()  # type: ignore[abstract]

    def test_full_implementation_instantiates(self) -> None:
        # _RecordingFactory is a complete implementation; if ABC shape is
        # correct, it must instantiate without error.
        factory = _RecordingFactory()
        assert isinstance(factory, BackendFactory)


# ---------------------------------------------------------------------------
# Sanity: CachingBackendProvider IS-A BackendProvider
# ---------------------------------------------------------------------------


def test_caching_backend_provider_is_a_backend_provider() -> None:
    factory = _RecordingFactory()
    provider = CachingBackendProvider(factory)
    assert isinstance(provider, BackendProvider)


def test_max_warm_backends_default_is_ten() -> None:
    assert CachingBackendProvider.MAX_WARM_BACKENDS == 10
