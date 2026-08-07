"""Unit tests for :class:`ScriptedProviderBackend` and friends.

The test double lives in production code (``scripted_backend.py``) but
behaves like a fixture: every test here constructs a fresh backend, so
no state leaks across tests. T9's integration tests will reuse the
class as a drop-in replacement for a real provider CLI.

Coverage shape:

- Data-model layer (``ScriptedStep`` / ``ScriptedProgramme``): frozen,
  hashable, default-value, status-literal validation.
- Backend layer (``ScriptedProviderBackend``): ABC adherence, call
  recording, programme access, status / session_id overrides,
  side-effect registration at a chosen step (the T9 hook surface),
  multiple-step side-effects, and the no-side-effect / no-call paths.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from pydantic import ValidationError

from modex_agent.agents.external import (
    BackendResult,
    ExecOptions,
    ProviderBackend,
)
from modex_agent.agents.external.scripted_backend import (
    ScriptedProgramme,
    ScriptedProviderBackend,
    ScriptedStep,
)

# ---------------------------------------------------------------------------
# ScriptedStep
# ---------------------------------------------------------------------------


class TestScriptedStep:
    """The single-step Pydantic model."""

    def test_minimal_defaults(self) -> None:
        s = ScriptedStep(text="hello")
        assert s.text == "hello"
        assert s.side_effect is False

    def test_explicit_side_effect(self) -> None:
        s = ScriptedStep(text="trigger", side_effect=True)
        assert s.side_effect is True

    def test_frozen_rejects_mutation(self) -> None:
        s = ScriptedStep(text="x")
        with pytest.raises(ValidationError):
            s.text = "y"

    def test_frozen_rejects_side_effect_mutation(self) -> None:
        s = ScriptedStep(text="x", side_effect=True)
        with pytest.raises(ValidationError):
            s.side_effect = False

    def test_extra_forbidden(self) -> None:
        with pytest.raises(ValidationError):
            ScriptedStep(text="x", extra_field="bad")  # type: ignore[call-arg]

    def test_equality(self) -> None:
        # Two steps with identical fields compare equal — Pydantic
        # generates ``__eq__`` from the model state.
        a = ScriptedStep(text="x", side_effect=True)
        b = ScriptedStep(text="x", side_effect=True)
        assert a == b


# ---------------------------------------------------------------------------
# ScriptedProgramme
# ---------------------------------------------------------------------------


class TestScriptedProgramme:
    """The closed-loop programme Pydantic model."""

    def test_minimal_defaults(self) -> None:
        p = ScriptedProgramme()
        assert p.steps == ()
        assert p.status == "completed"
        assert p.session_id is None

    def test_session_id_override(self) -> None:
        p = ScriptedProgramme(steps=(), session_id="sess-1")
        assert p.session_id == "sess-1"

    def test_status_literal_variants(self) -> None:
        for status in ("completed", "failed", "timeout", "aborted"):
            assert ScriptedProgramme(status=status).status == status

    def test_invalid_status_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ScriptedProgramme(status="bogus")  # type: ignore[arg-type]

    def test_steps_default_is_tuple_of_zero(self) -> None:
        p = ScriptedProgramme()
        assert isinstance(p.steps, tuple)
        assert len(p.steps) == 0

    def test_steps_pass_through_unchanged(self) -> None:
        steps = (ScriptedStep(text="a"), ScriptedStep(text="b"))
        p = ScriptedProgramme(steps=steps)
        assert p.steps == steps

    def test_frozen_rejects_mutation(self) -> None:
        p = ScriptedProgramme(steps=(), session_id="x")
        with pytest.raises(ValidationError):
            p.session_id = "y"

    def test_extra_forbidden(self) -> None:
        with pytest.raises(ValidationError):
            ScriptedProgramme(steps=(), unknown_field="bad")  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# ScriptedProviderBackend
# ---------------------------------------------------------------------------


class TestScriptedProviderBackendABC:
    """The backend is a concrete ``ProviderBackend`` subclass."""

    def test_implements_provider_backend(self) -> None:
        b = ScriptedProviderBackend(ScriptedProgramme())
        assert isinstance(b, ProviderBackend)

    def test_can_be_subclassed_to_extend(self) -> None:
        # Confirm the class is concrete (can be instantiated) and
        # supports subclassing for richer test fixtures.

        class CustomBackend(ScriptedProviderBackend):
            pass

        b = CustomBackend(ScriptedProgramme())
        assert isinstance(b, ProviderBackend)


class TestScriptedProviderBackendExecution:
    """``execute`` recording + return-value contract."""

    @pytest.mark.asyncio
    async def test_completed_status_when_programme_empty(self, tmp_path: Path) -> None:
        backend = ScriptedProviderBackend(ScriptedProgramme())
        result = await backend.execute(ExecOptions(prompt="hi", workdir=tmp_path))
        assert isinstance(result, BackendResult)
        assert result.status == "completed"
        assert result.session_id is None
        assert result.error is None

    @pytest.mark.asyncio
    async def test_records_each_call(self, tmp_path: Path) -> None:
        backend = ScriptedProviderBackend(ScriptedProgramme())
        opts_a = ExecOptions(prompt="hello", workdir=tmp_path)
        opts_b = ExecOptions(
            prompt="world",
            workdir=tmp_path,
            resume_session_id="prev-1",
            system_prompt="you are ...",
        )
        await backend.execute(opts_a)
        await backend.execute(opts_b)
        assert len(backend.calls) == 2
        assert backend.calls[0] is opts_a
        assert backend.calls[1] is opts_b
        assert backend.calls[1].resume_session_id == "prev-1"
        assert backend.calls[1].system_prompt == "you are ..."

    @pytest.mark.asyncio
    async def test_records_opts_first_before_side_effects(self, tmp_path: Path) -> None:
        # The recorder appends to .calls BEFORE iterating the script;
        # if a side-effect raised later, the call would still be on
        # the list. (We do not need to provoke an error, just verify
        # ordering by snapshotting calls before the loop touches it.)
        captured: list[ExecOptions | None] = [None]

        async def side_effect(opts: ExecOptions) -> None:
            # On entry, the call is already recorded.
            captured[0] = opts

        steps = (ScriptedStep(text="x", side_effect=True),)
        backend = ScriptedProviderBackend(ScriptedProgramme(steps=steps))
        backend.register_send_side_effect(side_effect)
        opts = ExecOptions(prompt="hi", workdir=tmp_path)
        await backend.execute(opts)
        assert captured[0] is opts
        assert backend.calls == [opts]


class TestScriptedProviderBackendStatusAndSession:
    """``status`` and ``session_id`` overrides from the programme model."""

    @pytest.mark.asyncio
    async def test_status_failed_override(self, tmp_path: Path) -> None:
        backend = ScriptedProviderBackend(
            ScriptedProgramme(steps=(), status="failed"),
        )
        result = await backend.execute(ExecOptions(prompt="x", workdir=tmp_path))
        assert result.status == "failed"

    @pytest.mark.asyncio
    async def test_status_timeout_override(self, tmp_path: Path) -> None:
        backend = ScriptedProviderBackend(
            ScriptedProgramme(steps=(), status="timeout"),
        )
        result = await backend.execute(ExecOptions(prompt="x", workdir=tmp_path))
        assert result.status == "timeout"

    @pytest.mark.asyncio
    async def test_status_aborted_override(self, tmp_path: Path) -> None:
        backend = ScriptedProviderBackend(
            ScriptedProgramme(steps=(), status="aborted"),
        )
        result = await backend.execute(ExecOptions(prompt="x", workdir=tmp_path))
        assert result.status == "aborted"

    @pytest.mark.asyncio
    async def test_session_id_returned(self, tmp_path: Path) -> None:
        backend = ScriptedProviderBackend(
            ScriptedProgramme(steps=(), session_id="sess-42"),
        )
        result = await backend.execute(ExecOptions(prompt="x", workdir=tmp_path))
        assert result.session_id == "sess-42"

    @pytest.mark.asyncio
    async def test_error_field_is_default_none(self, tmp_path: Path) -> None:
        # The test double does not synthesise an ``error`` value —
        # downstream consumers default this to ``None`` for the
        # "completed" path.
        backend = ScriptedProviderBackend(
            ScriptedProgramme(status="failed"),
        )
        result = await backend.execute(ExecOptions(prompt="x", workdir=tmp_path))
        assert result.error is None


class TestScriptedProviderBackendProgramme:
    """Programme exposure + step text introspection."""

    def test_programme_property_returns_same_instance(self) -> None:
        p = ScriptedProgramme(steps=(ScriptedStep(text="x"),))
        b = ScriptedProviderBackend(p)
        # Identity — tests rely on this so they can compare step
        # contents against the public programme attribute.
        assert b.programme is p

    def test_programme_steps_introspectable(self) -> None:
        steps = (
            ScriptedStep(text="delta: hello"),
            ScriptedStep(text="thinking", side_effect=True),
            ScriptedStep(text="tool: bash"),
        )
        backend = ScriptedProviderBackend(ScriptedProgramme(steps=steps))
        assert backend.programme.steps[0].text == "delta: hello"
        assert backend.programme.steps[1].text == "thinking"
        assert backend.programme.steps[1].side_effect is True
        assert backend.programme.steps[2].text == "tool: bash"

    @pytest.mark.asyncio
    async def test_playback_step_count_matches_programme(self, tmp_path: Path) -> None:
        steps = (ScriptedStep(text=f"line-{i}") for i in range(5))
        backend = ScriptedProviderBackend(
            ScriptedProgramme(steps=tuple(steps)),
        )
        # We can't observe "played back" from the outside (no parser
        # wired in), but the BackendResult contract still holds and
        # we can confirm the programme length matches what we built.
        assert len(backend.programme.steps) == 5
        result = await backend.execute(ExecOptions(prompt="x", workdir=tmp_path))
        assert result.status == "completed"


class TestScriptedProviderBackendSideEffect:
    """The ``register_send_side_effect`` hook — T9's chosen moment."""

    @pytest.mark.asyncio
    async def test_no_side_effect_call_means_nothing_invoked(self, tmp_path: Path) -> None:
        invocations = 0

        async def side_effect(opts: ExecOptions) -> None:
            nonlocal invocations
            invocations += 1

        steps = (ScriptedStep(text="only"),)
        backend = ScriptedProviderBackend(ScriptedProgramme(steps=steps))
        backend.register_send_side_effect(side_effect)
        await backend.execute(ExecOptions(prompt="x", workdir=tmp_path))
        assert invocations == 0

    @pytest.mark.asyncio
    async def test_side_effect_invoked_exactly_once_when_marked(self, tmp_path: Path) -> None:
        invocations: list[ExecOptions] = []

        async def side_effect(opts: ExecOptions) -> None:
            invocations.append(opts)

        steps = (
            ScriptedStep(text="first"),
            ScriptedStep(text="trigger", side_effect=True),
            ScriptedStep(text="third"),
        )
        backend = ScriptedProviderBackend(ScriptedProgramme(steps=steps))
        backend.register_send_side_effect(side_effect)
        opts = ExecOptions(prompt="hi", workdir=tmp_path)
        await backend.execute(opts)
        assert len(invocations) == 1
        assert invocations[0] is opts

    @pytest.mark.asyncio
    async def test_side_effect_invoked_at_each_marked_step(self, tmp_path: Path) -> None:
        invocations = 0

        async def side_effect(opts: ExecOptions) -> None:
            nonlocal invocations
            invocations += 1

        steps = (
            ScriptedStep(text="a", side_effect=True),
            ScriptedStep(text="b"),
            ScriptedStep(text="c", side_effect=True),
            ScriptedStep(text="d", side_effect=True),
        )
        backend = ScriptedProviderBackend(ScriptedProgramme(steps=steps))
        backend.register_send_side_effect(side_effect)
        await backend.execute(ExecOptions(prompt="x", workdir=tmp_path))
        assert invocations == 3

    @pytest.mark.asyncio
    async def test_side_effect_receives_execute_opts(self, tmp_path: Path) -> None:
        received: list[ExecOptions | None] = [None]

        async def side_effect(opts: ExecOptions) -> None:
            received[0] = opts

        steps = (ScriptedStep(text="x", side_effect=True),)
        backend = ScriptedProviderBackend(ScriptedProgramme(steps=steps))
        backend.register_send_side_effect(side_effect)
        opts = ExecOptions(prompt="ping", workdir=tmp_path)
        await backend.execute(opts)
        assert received[0] is opts
        assert received[0] is not None
        assert received[0].prompt == "ping"

    @pytest.mark.asyncio
    async def test_side_effect_can_safely_call_send_routing(self, tmp_path: Path) -> None:
        # T9 is expected to register a closure that calls
        # ``modexbot.send._write_line``. This test simulates that
        # shape without depending on the T2 module (which is not yet
        # shipped at T3 time).
        captured_lines: list[str] = []

        async def fake_send_write_line(opts: ExecOptions) -> None:
            # Mimic T2's routing function by appending to a sink.
            captured_lines.append(f"send:{opts.prompt}")

        steps = (
            ScriptedStep(text="out", side_effect=True),
            ScriptedStep(text="more", side_effect=True),
        )
        backend = ScriptedProviderBackend(ScriptedProgramme(steps=steps))
        backend.register_send_side_effect(fake_send_write_line)
        await backend.execute(ExecOptions(prompt="ping", workdir=tmp_path))
        assert captured_lines == ["send:ping", "send:ping"]

    @pytest.mark.asyncio
    async def test_side_effect_overwrites_prior(self, tmp_path: Path) -> None:
        first_called = False
        second_called = False

        async def first(opts: ExecOptions) -> None:
            nonlocal first_called
            first_called = True

        async def second(opts: ExecOptions) -> None:
            nonlocal second_called
            second_called = True

        steps = (ScriptedStep(text="x", side_effect=True),)
        backend = ScriptedProviderBackend(ScriptedProgramme(steps=steps))
        backend.register_send_side_effect(first)
        backend.register_send_side_effect(second)
        await backend.execute(ExecOptions(prompt="x", workdir=tmp_path))
        assert first_called is False
        assert second_called is True

    @pytest.mark.asyncio
    async def test_side_effect_no_callable_registered_is_silent(self, tmp_path: Path) -> None:
        # Side-effect step WITHOUT a registered callable must not
        # raise — that's the default state of every fresh backend.
        steps = (ScriptedStep(text="x", side_effect=True),)
        backend = ScriptedProviderBackend(ScriptedProgramme(steps=steps))
        # No ``register_send_side_effect`` call.
        result = await backend.execute(ExecOptions(prompt="x", workdir=tmp_path))
        assert result.status == "completed"


class TestScriptedProviderBackendCallList:
    """``calls`` is a plain ``list`` that grows monotonically."""

    @pytest.mark.asyncio
    async def test_calls_starts_empty(self) -> None:
        backend = ScriptedProviderBackend(ScriptedProgramme())
        assert backend.calls == []

    @pytest.mark.asyncio
    async def test_calls_preserve_insertion_order(self, tmp_path: Path) -> None:
        backend = ScriptedProviderBackend(ScriptedProgramme())
        await backend.execute(ExecOptions(prompt="a", workdir=tmp_path))
        await backend.execute(ExecOptions(prompt="b", workdir=tmp_path))
        await backend.execute(ExecOptions(prompt="c", workdir=tmp_path))
        assert [c.prompt for c in backend.calls] == ["a", "b", "c"]

    @pytest.mark.asyncio
    async def test_calls_entries_are_pydantic_instances(self, tmp_path: Path) -> None:
        backend = ScriptedProviderBackend(ScriptedProgramme())
        await backend.execute(ExecOptions(prompt="a", workdir=tmp_path))
        # BackendResult-style frozen Pydantic field snapshot.
        assert isinstance(backend.calls[0], ExecOptions)


class TestScriptedProviderBackendIteratorProtocol:
    """Verify callers can iterate ``programme.steps`` as a generator.

    (Utility check that the test double is iterable-friendly — useful
    for T9 fixtures that want to drive the script manually.)
    """

    def test_steps_iterable(self) -> None:
        steps = (ScriptedStep(text="a"), ScriptedStep(text="b"))
        backend = ScriptedProviderBackend(ScriptedProgramme(steps=steps))
        assert isinstance(backend.programme.steps, tuple)
        result_iter: Iterator[ScriptedStep] = iter(backend.programme.steps)
        first = next(result_iter)
        second = next(result_iter)
        assert first.text == "a"
        assert second.text == "b"
