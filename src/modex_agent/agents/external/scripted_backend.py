"""``ScriptedProviderBackend`` — an in-memory test double for ``ProviderBackend``.

Lives under ``src/`` (not ``tests/``) because every T9 integration test
imports it from production code paths to stand in for a real CLI.
"Same routing code path the real CLI uses, only the subprocess
boundary is faked" — the registered side-effect callable is invoked at
a chosen ``ScriptedStep``, so T9 can plug in ``modexbot.send._write_line``
to drive the real outbox writer from inside the script.

Scope discipline:

- Records each invocation's ``ExecOptions`` (the only contract surface
  ``ProviderBackend`` exposes) plus exposes ``.calls`` for
  fan-out assertions.
- Plays back the step list in order; ``side_effect=True`` steps invoke
  the registered callable with the captured ``ExecOptions`` so T9 can
  exercise downstream routing synchronously.
- Returns a ``BackendResult`` whose ``status`` and ``session_id``
  fields come from the :class:`ScriptedProgramme` (with sensible
  defaults: ``"completed"`` / ``None``).
- Holds NO state between ``execute`` calls beyond ``.calls`` —
  concurrent calls would race, but every T9 test uses one-and-done.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from pydantic import BaseModel, ConfigDict

from .contracts import ProviderBackend
from .types import BackendResult, BackendStatus, ExecOptions

# ---------------------------------------------------------------------------
# Scripted step / programme models
# ---------------------------------------------------------------------------


class ScriptedStep(BaseModel):
    """A single step in a :class:`ScriptedProviderBackend` playback.

    Attributes:
        text: The line the backend "emits" at this step (observable via
            ``backend.programme.steps[i].text`` from tests — the test
            double itself does not surface it back through a callback,
            consistent with the ABC contract that does not carry
            per-step emissions).
        side_effect: When ``True``, the backend awaits the registered
            side-effect callable with the captured ``ExecOptions``
            BEFORE yielding the loop. Defaults to ``False`` so the
            common case (pure playback) reads cleanly.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    text: str
    side_effect: bool = False


class ScriptedProgramme(BaseModel):
    """The closed-loop event sequence for ``ScriptedProviderBackend``.

    Attributes:
        steps: Ordered tuple of :class:`ScriptedStep` records. Empty
            means a no-step "happy completion" run.
        status: ``BackendResult.status`` value to return on completion.
            Must be one of the four closed literal members enforced by
            :class:`BackendResult`.
        session_id: ``BackendResult.session_id`` to return (e.g. the
            "next" provider session id for resume support, or ``None``
            for fresh sessions / empty programmes).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    steps: tuple[ScriptedStep, ...] = ()
    status: BackendStatus = BackendStatus.COMPLETED
    session_id: str | None = None


# ---------------------------------------------------------------------------
# Type alias for the registered side-effect callable
# ---------------------------------------------------------------------------


# A callable that T9 plugs in — typically a wrapper around
# ``modexbot.send._write_line`` (or equivalent routing function from
# T2) the real CLI's bash tool would invoke at runtime.
SendSideEffect = Callable[[ExecOptions], Awaitable[None]]


# ---------------------------------------------------------------------------
# ScriptedProviderBackend
# ---------------------------------------------------------------------------


class ScriptedProviderBackend(ProviderBackend):
    """``ProviderBackend`` test double — records + plays back + side-effects.

    The class implements the ABC by extending it directly (concrete
    ``execute``). State held across calls is just the captured
    ``ExecOptions`` list (``.calls``) and the optional side-effect
    callable. Subclasses for richer scripts override :meth:`execute` and
    call ``super().execute(opts)`` to retain the side-effect wiring.

    Threading: every call must run in the same asyncio loop because
    ``ScriptedProviderBackend`` is not thread-safe (no ``asyncio.Lock``
    around ``.calls``).
    """

    def __init__(self, programme: ScriptedProgramme) -> None:
        super().__init__()
        self._programme: ScriptedProgramme = programme
        self._send_side_effect: SendSideEffect | None = None
        self._calls: list[ExecOptions] = []

    @property
    def programme(self) -> ScriptedProgramme:
        """The programme this backend is replaying.

        Tests use this to inspect the step text the backend would emit,
        because ``ProviderBackend.execute`` returns a terminal
        ``BackendResult`` with no per-line surface.
        """
        return self._programme

    @property
    def calls(self) -> list[ExecOptions]:
        """Every ``ExecOptions`` instance ``execute`` has been called with.

        Each entry is the exact Pydantic model the caller passed (frozen
        guarantee preserved — we never mutate, only append). Tests
        assert against this list to verify routing correctness.
        """
        return self._calls

    def register_send_side_effect(self, fn: SendSideEffect) -> None:
        """Register an async callable invoked at every step marked ``side_effect=True``.

        The callable receives the captured ``ExecOptions`` and is awaited
        sequentially before the next step plays. T9's expected caller is
        a closure wrapping T2's ``modexbot.send._write_line`` so the
        outbox writer runs against the real ``pending.jsonl``.

        Args:
            fn: ``Callable[[ExecOptions], Awaitable[None]]``. Replaces
                any previously-registered callable.
        """
        self._send_side_effect = fn

    async def execute(self, opts: ExecOptions) -> BackendResult:
        """Replay the programme against ``opts`` and return its result.

        Records ``opts`` first so tests can verify the call sequence
        before any side effects run. Each :class:`ScriptedStep` is
        consumed left-to-right; side-effect calls happen between
        consecutive steps so a side-effect at step ``i`` runs AFTER the
        step-``i`` "output" was emitted (mirroring the real CLI's
        "output-then-trigger-tool" pattern).

        Args:
            opts: Per-spawn execution options. The script records them
                verbatim — tests inspect ``workdir``, ``prompt``, and
                ``resume_session_id`` to verify routing.
        Returns:
            ``BackendResult(status=programme.status, session_id=programme.session_id)``.
        """
        self._calls.append(opts)

        for step in self._programme.steps:
            # Side-effect fires AFTER this step's "output" — matches
            # the real CLI cadence (provider emits a line, the LLM
            # decides to call ``modexctl send`` as a tool, the next
            # step's output resumes).
            if step.side_effect and self._send_side_effect is not None:
                await self._send_side_effect(opts)
            # Yield once so a downstream coroutine can interleave
            # (matters for tests that rely on deterministic ordering).
            await asyncio.sleep(0)

        return BackendResult(
            status=self._programme.status,
            session_id=self._programme.session_id,
        )


__all__ = [
    "ScriptedStep",
    "ScriptedProgramme",
    "ScriptedProviderBackend",
    "SendSideEffect",
]
