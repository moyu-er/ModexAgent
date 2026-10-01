"""Unit tests for :class:`ScriptedTransport` — the transport test double.

The double lives in production code (``transports/scripted.py``) but
behaves like a fixture: every test here constructs a fresh transport, so
no state leaks across tests. Integration tests reuse the class as a
drop-in replacement for a real provider CLI.

Coverage shape:

- Data-model layer (``ScriptedStep`` / ``ScriptedProgramme``): frozen,
  default values, status validation.
- Transport layer: ABC adherence, call recording, programme access,
  status / session_id overrides, side-effect registration at a chosen
  step, child-session callback demux, and the error raised when a child
  step runs without a child factory.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from modex_agent.agents.external.transports import (
    ExternalTransport,
    ScriptedProgramme,
    ScriptedStep,
    ScriptedTransport,
)
from modex_agent.agents.external.types import BackendStatus, ExecOptions
from modex_agent.core.turn_events import (
    TurnEvent,
    TurnTextEvent,
    TurnToolCallEvent,
    TurnToolResultEvent,
)

# ---------------------------------------------------------------------------
# ScriptedStep
# ---------------------------------------------------------------------------


class TestScriptedStep:
    """The single-step Pydantic model."""

    def test_minimal_defaults(self) -> None:
        s = ScriptedStep()
        assert s.event is None
        assert s.source_session_id is None
        assert s.side_effect is False

    def test_explicit_fields(self) -> None:
        s = ScriptedStep(
            event=TurnTextEvent(text="hi"),
            source_session_id="child_1",
            side_effect=True,
        )
        assert s.event == TurnTextEvent(text="hi")
        assert s.source_session_id == "child_1"
        assert s.side_effect is True

    def test_frozen_rejects_mutation(self) -> None:
        s = ScriptedStep(event=TurnTextEvent(text="x"))
        with pytest.raises(ValidationError):
            s.event = TurnTextEvent(text="y")  # type: ignore[misc]

    def test_extra_forbidden(self) -> None:
        with pytest.raises(ValidationError):
            ScriptedStep(text="x")  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# ScriptedProgramme
# ---------------------------------------------------------------------------


class TestScriptedProgramme:
    """The closed-loop programme Pydantic model."""

    def test_minimal_defaults(self) -> None:
        p = ScriptedProgramme()
        assert p.steps == ()
        assert p.status == BackendStatus.COMPLETED
        assert p.session_id is None

    def test_status_literal_variants(self) -> None:
        for status in ("completed", "failed", "timeout", "aborted"):
            assert ScriptedProgramme(status=status).status == status

    def test_invalid_status_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ScriptedProgramme(status="bogus")  # type: ignore[arg-type]

    def test_steps_pass_through_unchanged(self) -> None:
        steps = (ScriptedStep(event=TurnTextEvent(text="a")), ScriptedStep(side_effect=True))
        p = ScriptedProgramme(steps=steps)
        assert p.steps == steps

    def test_frozen_rejects_mutation(self) -> None:
        p = ScriptedProgramme(session_id="x")
        with pytest.raises(ValidationError):
            p.session_id = "y"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# ScriptedTransport
# ---------------------------------------------------------------------------


def _collect(sink: list[TurnEvent]):
    async def _cb(event: TurnEvent) -> None:
        sink.append(event)

    return _cb


class TestScriptedTransportABC:
    def test_implements_external_transport(self) -> None:
        transport = ScriptedTransport(ScriptedProgramme())
        assert isinstance(transport, ExternalTransport)

    def test_can_be_subclassed_to_extend(self) -> None:
        class Custom(ScriptedTransport):
            pass

        assert isinstance(Custom(ScriptedProgramme()), ExternalTransport)


class TestScriptedTransportExecution:
    async def test_completed_status_when_programme_empty(self, tmp_path: Path) -> None:
        transport = ScriptedTransport(ScriptedProgramme())
        result = await transport.execute(
            ExecOptions(prompt="hi", workdir=tmp_path), {}, _collect([])
        )
        assert result.status == BackendStatus.COMPLETED
        assert result.session_id is None
        assert result.error is None

    async def test_records_each_call(self, tmp_path: Path) -> None:
        transport = ScriptedTransport(ScriptedProgramme())
        opts_a = ExecOptions(prompt="hello", workdir=tmp_path)
        opts_b = ExecOptions(
            prompt="world",
            workdir=tmp_path,
            resume_session_id="prev-1",
        )
        await transport.execute(opts_a, {"A": "1"}, _collect([]))
        await transport.execute(opts_b, {"B": "2"}, _collect([]))
        assert transport.recorded_opts == [opts_a, opts_b]
        assert transport.recorded_envs == [{"A": "1"}, {"B": "2"}]
        assert transport.recorded_opts[1].resume_session_id == "prev-1"

    async def test_events_delivered_in_order_to_main_callback(self, tmp_path: Path) -> None:
        received: list[TurnEvent] = []
        programme = ScriptedProgramme(
            steps=(
                ScriptedStep(event=TurnTextEvent(text="one")),
                ScriptedStep(event=TurnToolCallEvent(tool_name="bash", call_id="c1", arguments={})),
                ScriptedStep(event=TurnToolResultEvent(tool_name="bash", call_id="c1", output="ok")),
            )
        )
        await ScriptedTransport(programme).execute(
            ExecOptions(prompt="hi", workdir=tmp_path), {}, _collect(received)
        )
        assert [type(e) for e in received] == [
            TurnTextEvent,
            TurnToolCallEvent,
            TurnToolResultEvent,
        ]

    async def test_child_events_delivered_via_child_factory(self, tmp_path: Path) -> None:
        main: list[TurnEvent] = []
        child_a: list[TurnEvent] = []
        child_b: list[TurnEvent] = []
        factories: dict[str, list[TurnEvent]] = {"child_a": child_a, "child_b": child_b}

        def child_factory(provider_sid: str):
            return _collect(factories[provider_sid])

        programme = ScriptedProgramme(
            steps=(
                ScriptedStep(event=TurnTextEvent(text="main")),
                ScriptedStep(event=TurnTextEvent(text="a"), source_session_id="child_a"),
                ScriptedStep(event=TurnTextEvent(text="b"), source_session_id="child_b"),
                ScriptedStep(event=TurnTextEvent(text="a2"), source_session_id="child_a"),
            )
        )
        await ScriptedTransport(programme).execute(
            ExecOptions(prompt="hi", workdir=tmp_path),
            {},
            _collect(main),
            child_factory,
        )
        assert main == [TurnTextEvent(text="main")]
        assert child_a == [TurnTextEvent(text="a"), TurnTextEvent(text="a2")]
        assert child_b == [TurnTextEvent(text="b")]

    async def test_child_step_without_factory_raises(self, tmp_path: Path) -> None:
        programme = ScriptedProgramme(
            steps=(ScriptedStep(event=TurnTextEvent(text="x"), source_session_id="c1"),)
        )
        with pytest.raises(ValueError, match="on_child_event"):
            await ScriptedTransport(programme).execute(
                ExecOptions(prompt="hi", workdir=tmp_path), {}, _collect([])
            )

    async def test_status_and_session_overrides(self, tmp_path: Path) -> None:
        for status in (BackendStatus.FAILED, BackendStatus.TIMEOUT, BackendStatus.ABORTED):
            transport = ScriptedTransport(
                ScriptedProgramme(status=status, session_id="sess-42")
            )
            result = await transport.execute(
                ExecOptions(prompt="x", workdir=tmp_path), {}, _collect([])
            )
            assert result.status == status
            assert result.session_id == "sess-42"


class TestScriptedTransportSideEffect:
    async def test_no_side_effect_call_means_nothing_invoked(self, tmp_path: Path) -> None:
        invocations = 0

        async def side_effect(opts: ExecOptions) -> None:
            nonlocal invocations
            invocations += 1

        transport = ScriptedTransport(
            ScriptedProgramme(steps=(ScriptedStep(event=TurnTextEvent(text="only")),)),
            send_side_effect=side_effect,
        )
        await transport.execute(ExecOptions(prompt="x", workdir=tmp_path), {}, _collect([]))
        assert invocations == 0

    async def test_side_effect_invoked_at_each_marked_step(self, tmp_path: Path) -> None:
        invocations: list[ExecOptions] = []

        async def side_effect(opts: ExecOptions) -> None:
            invocations.append(opts)

        programme = ScriptedProgramme(
            steps=(
                ScriptedStep(event=TurnTextEvent(text="a"), side_effect=True),
                ScriptedStep(event=TurnTextEvent(text="b")),
                ScriptedStep(event=TurnTextEvent(text="c"), side_effect=True),
                ScriptedStep(side_effect=True),
            )
        )
        transport = ScriptedTransport(programme, send_side_effect=side_effect)
        opts = ExecOptions(prompt="hi", workdir=tmp_path)
        await transport.execute(opts, {}, _collect([]))
        assert len(invocations) == 3
        assert invocations[0] is opts

    async def test_side_effect_without_callable_registered_is_silent(self, tmp_path: Path) -> None:
        programme = ScriptedProgramme(steps=(ScriptedStep(side_effect=True),))
        result = await ScriptedTransport(programme).execute(
            ExecOptions(prompt="x", workdir=tmp_path), {}, _collect([])
        )
        assert result.status == BackendStatus.COMPLETED

    async def test_register_send_side_effect_overwrites_prior(self, tmp_path: Path) -> None:
        first_called = False
        second_called = False

        async def first(opts: ExecOptions) -> None:
            nonlocal first_called
            first_called = True

        async def second(opts: ExecOptions) -> None:
            nonlocal second_called
            second_called = True

        transport = ScriptedTransport(
            ScriptedProgramme(steps=(ScriptedStep(side_effect=True),))
        )
        transport.register_send_side_effect(first)
        transport.register_send_side_effect(second)
        await transport.execute(ExecOptions(prompt="x", workdir=tmp_path), {}, _collect([]))
        assert first_called is False
        assert second_called is True


class TestScriptedTransportProgramme:
    def test_programme_property_returns_same_instance(self) -> None:
        p = ScriptedProgramme(steps=(ScriptedStep(event=TurnTextEvent(text="x")),))
        transport = ScriptedTransport(p)
        assert transport.programme is p

    def test_programme_steps_introspectable(self) -> None:
        steps = (
            ScriptedStep(event=TurnTextEvent(text="delta")),
            ScriptedStep(side_effect=True),
            ScriptedStep(event=TurnToolCallEvent(tool_name="bash", call_id="c", arguments={})),
        )
        transport = ScriptedTransport(ScriptedProgramme(steps=steps))
        assert transport.programme.steps[0].event == TurnTextEvent(text="delta")
        assert transport.programme.steps[1].side_effect is True
        assert transport.programme.steps[2].event is not None
