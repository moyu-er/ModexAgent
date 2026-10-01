# Unified Turn-Event Stream

**Status**: Accepted (implemented end to end — the staged rollout completed; this record now describes the as-built system)
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

### 4. Staged rollout — completed (as landed)

- **Wave 1** — union extension; ``StopReason`` relocation; the projector consumes core events; signal deletion; architecture anchors (projector disposition completeness, translator table exhaustiveness, entry validity) plus the union/validator unit tests.
- **Runtime emission cutover** — the native graph nodes construct core events at the source; the enum-channel translators are gone.
- **Sink face** — a single per-turn ``TurnEventSink`` (+ ``TurnBinding``) replaces the ``ContentEmitter`` method channel; ``BufferingSink`` sinks an ``OutputAdapter`` onto the stream under a ``DeliveryPolicy``.
- **Presentation hub** — ``SessionEventHub`` owns projection + fan-out to registered ``PresentationSink`` consumers (the bot's transcript tap and wire projection are both hub consumers).
- **External transport adapters** — CLI/ACP/SDK transports map their private wire formats onto the union through one shared normalizer.
- **Approval / usage producers** — the approval suspension/resume points and the LLM finishing path emit ``approval_requested`` / ``approval_resolved`` / ``usage`` (a zero-producer anchor proves every union kind has a default producer).
- **Transcript record cutover** — presentation events ARE the durable transcript records; the bot's parallel materializer is gone and replay goes through the framework's single ``materialize_turns``.
- **Consumer realignment** — the editor-protocol path consumes presentation events directly: ``AcpInteraction.emit_presentation`` carries the projected stream and ``events_map`` maps ``PresentationEvent`` → session updates (the raw ``TurnEvent`` input stays only for the framework's scripted backend). The re-projection loop is removed.

## As-Built Decisions (refinements recorded at completion)

- **Async sink emission is a sequential await.** ``TurnEventSink.emit`` awaits each consumer's handler in order — delivery order is the observable contract, matching the pre-existing emitter method channel (no fan-out task spawning, no buffering between producer and consumer).
- **One terminal kind.** ``TurnFinishedEvent`` carries ``StopReason`` + optional ``error`` AND ``attachments`` — output artifacts are observable facts of the finished turn, so no second terminal variant exists for them.
- **``TurnErroredEvent`` stays a mid-turn observation.** An error surfaced mid-turn does not classify the turn; terminal classification still arrives only via ``turn_finished``.
- **Resume rebinds the same turn id.** An approval resume re-invokes the sink factory with a ``TurnBinding`` carrying the suspended attempt's ``turn_id`` and ``resumed=True``; the hub pre-activates the projector so the resumed leg continues the SAME turn and emits no second ``TurnStarted`` (neither eager nor lazy).
- **External child routing rides a typed child-callback factory.** Core events carry no session identity by design; a backend that must route a child session's stream to its owner injects a typed child-callback/``ChildSessionResolver`` at its own seam (the ACP emitter hub), never an identity field on the union.
- **``TurnStarted`` is not persisted, and the transient kind set is explicit.** Turn grouping rides the record identity envelope (``turn_id``) alone; ``tool_args_delta`` (warm-up) never touches a transcript store. The transient kinds are a declared set, not an implicit omission.
- **Transcript record generation detection has a single legacy conversion point.** A store reading old-format lines adapts them in its codec's ``parse`` (zero-or-more records per line) — read-time adaptation in one place, never a parallel writer.
- **Interruption stays single-taxonomy.** Cancellation/interruption classifies exclusively through ``StopReason`` on ``turn_finished``; the react-internal interrupted marking never surfaces as a separate event kind (``TurnInterrupted`` / ``TurnResumed`` remain deleted).

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
- The rollout is machine-proven by gates that now exist in the suite:
  - **disposition anchor** — the projector's mapped ∪ ignored tables must equal the full core kind set (an appended variant without a declared disposition fails the build);
  - **zero-producer anchor** — every union kind must have a default runtime producer (a kind with no producer fails, so dead vocabulary cannot re-enter);
  - **seam guards** — the sink/binding contract and the hub's consumer ordering are pinned by unit tests, and the scripted/editor interaction altitudes share one wire map (``events_map``), so a second mapping table cannot silently diverge;
  - **replay equivalence** — a scripted live stream and its materialized transcript round-trip to the same turn view (``materialize_turns``), pinning the transcript contract end to end.

### Negative / recorded honestly

- ``TurnFinishedEvent`` carries ``attachments`` that only the buffering sink delivers today; consumers that ignore them see no difference (the field is the durable record of the artifacts, delivery is each sink's concern).
- The validator stays lenient by default because bridge streams may still start with content rather than ``turn_started``; strict mode remains opt-in for test gates.
- ``AcpInteraction`` carries two input altitudes (``emit`` core, ``emit_presentation`` projected) — the deliberate cost of keeping the SDK-free scripted backend on raw core events while the editor road consumes the projection; both converge on the same ``events_map`` wire models.

## Related

- ADR-0053 — the presentation projection layer this stream feeds
- ADR-0052 — runtime slotting (the prerequisite for converging the emitter seam)
- ADR-0046 — the closed-union / append-only discipline this union mirrors (``LLMStreamEvent``)
