# Unified Turn-Event Stream

**Status**: Accepted (Wave 1 landed; later waves update this record as they land)
**Date**: 2026-10-01

## Context

The framework runs two agent execution planes — the native ReAct graph runtime and the external coding-agent harness — but their runtime observations leave through divergent vocabularies:

- The native plane emits an agent-specific enum stream (``ReActEvent`` + untyped ``data: Any`` payloads) through ``ContentEmitter.emit``; the external plane emits the canonical core ``TurnEvent`` union through ``emit_turn_event``. The canonical seam the framework itself defined was bypassed by one of its own planes.
- Every presentation consumer therefore hand-rolls a translation from the enum stream onto neutral inputs (``BotTranscriptEmitter._on_event``), a table that must be kept in manual three-way sync with the projector's mapped/ignored lists and with any second consumer's copy.
- The presentation projector (ADR-0053) accumulated four private input signal classes (``ToolArgsDeltaSignal`` / ``TurnEndedSignal`` / ``TurnFailedSignal`` / ``UsageReportedSignal``) as patches for facts the core union did not carry: streamed argument fragments, terminal classification, error surfaces, usage snapshots.
- The presentation vocabulary carried kinds with zero producers and zero consumers (``TurnInterrupted`` / ``TurnResumed``; approval and usage had producers nowhere on the native path).
- The editor-protocol path re-projects presentation events back into ``TurnEvent``s before its own event map — a projection of a projection.
- The bot keeps a transcript materializer parallel to the framework's ``materialize_turns`` (two folders for the same turn shape).

The result: adding one observable fact (an approval, a usage snapshot, an iteration boundary) touches a private signal class, two hand-maintained disposition lists, every consumer translator, and possibly a second materializer — with no mechanical check that any of them stayed exhaustive.

## Decision

### 1. One closed, append-only runtime union in core — consumed by BOTH planes

``core/turn_events.py`` owns the full runtime vocabulary: the four content/tool variants plus ten lifecycle/observation variants — ``turn_started``, ``turn_finished`` (carrying ``StopReason``, optional ``error`` and ``attachments``), ``turn_errored`` (mid-turn observation; terminal classification still arrives via ``turn_finished``), ``tool_args_delta``, ``approval_requested`` / ``approval_resolved``, ``usage``, ``iteration_started`` / ``iteration_finished``, ``progress``. The union is frozen, ``extra="forbid"``, discriminated by ``kind``, and follows the same append-only discipline as ``LLMStreamEvent``: variants are only ever appended, never reshaped. Events carry no session/agent/turn identity — identity is assigned downstream by the presentation projector. ``StopReason`` moves here from ``core/emitter.py`` (single owner for the terminal taxonomy).

### 2. The presentation projector consumes the core union directly — the signal patches die

``DefaultTurnEventProjector.feed`` takes ``TurnEvent``. The four private signal classes and the ``RuntimeTurnEvent`` alias are deleted; their facts now arrive as core variants (``ToolArgsDeltaEvent`` / ``TurnFinishedEvent`` / ``TurnErroredEvent`` / ``UsageEvent``). The projector's disposition tables become kind-level ClassVars — ``MAPPED_TURN_EVENT_KINDS`` and ``IGNORED_TURN_EVENT_KINDS`` — whose union must equal the full core kind set (enforced by an architecture anchor). ``turn_started`` assigns turn identity eagerly; the lazy content-first path stays for bridge streams without ``turn_started``. ``TurnEventValidator`` (``core/turn_validator.py``) provides the per-session sequence state machine (lenient default / strict) for tests and debugging gates.

### 3. The presentation vocabulary drops its dead kinds

``TurnInterrupted`` and ``TurnResumed`` are deleted (zero producers, zero consumers): interruption classification rides ``TurnFinished.stop_reason``; resume observation rides ``ApprovalResolved``. The union shrinks 13 → 11 kinds.

### 4. Staged rollout (this ADR is updated as waves land)

- **Wave 1 (landed)** — union extension; ``StopReason`` relocation; projector consumes core events; signal deletion; bot translator (``BotTranscriptEmitter._on_event``) driven by a declarative ``_REACT_TO_TURN_KINDS`` table that emits core events; ``emit_complete`` feeds ``TurnFinishedEvent``; architecture anchors (projector disposition completeness, translator table exhaustiveness, entry validity) plus the union/validator unit tests. Behavior-preserving: WS wire frames, transcript records, the emitter method channel and the ``ReActEvent`` enum all stay alive and identical in this wave.
- **Runtime emission cutover** — the native graph nodes construct core events at the source; the enum channel's translators shrink to nothing.
- **Sink face** — a single per-turn event sink replaces the ``ContentEmitter`` method channel.
- **External transport adapters** — CLI/ACP/SDK transports map their private wire formats onto the union through one shared normalizer.
- **Approval / usage observation producers** — the approval suspension point and the LLM finishing path emit ``approval_requested`` / ``approval_resolved`` / ``usage``.
- **Transcript record cutover** — bot transcript records converge onto the framework's materialization (one folder).
- **Consumer realignment** — the editor-protocol path consumes presentation events directly; the re-projection loop is removed.

## Considered Options

- **Keep the enum channel + per-consumer translation** (status quo): cheapest tomorrow, but every new fact multiplies hand-maintained tables with no exhaustiveness check — rejected.
- **Widen the ``ContentEmitter`` method channel** (one method per fact): preserves the enum channel's untyped payload problem and adds a method per variant; discriminated-union dispatch is strictly simpler — rejected.
- **Per-plane unions with a shared superset**: two vocabularies to keep aligned forever; the superset IS the shared vocabulary — rejected as indirection without a consumer.

## Consequences

### Positive

- One runtime vocabulary for both planes; a new observable fact is one appended variant plus declared dispositions, and the architecture anchors fail loudly when a disposition or translator entry is missing.
- The projector's input is a public, typed, frozen union — its private signal patch surface is gone, and approval/usage now project through the same path as everything else.
- Dead presentation kinds are removed rather than documented as producer-less.
- ``StopReason`` lives beside the terminal event that carries it.

### Negative / recorded honestly

- During migration the native plane still emits its enum stream; the consumer-side translator (now declarative and anchored) remains until the runtime emission cutover — this is the explicitly staged cost of never breaking the wire in one step.
- ``TurnFinishedEvent`` carries ``attachments`` that no consumer reads yet (later waves deliver them through the sink face); the projector deliberately ignores them today.
- The validator is lenient by default because real bridge streams start with content, not ``turn_started``; strict mode becomes the default only after the emission cutover.

## Related

- ADR-0053 — the presentation projection layer this stream feeds
- ADR-0052 — runtime slotting (the prerequisite for converging the emitter seam)
- ADR-0046 — the closed-union / append-only discipline this union mirrors (``LLMStreamEvent``)
