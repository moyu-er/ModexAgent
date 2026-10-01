# Neutral Presentation Event Projection Layer

**Status**: Accepted
**Date**: 2026-10-01

## Context

The runtime already emits events through two neutral seams: the core-level ``TurnEvent`` family (``core/turn_events.py``, produced by external-coding agents via ``ContentEmitter.emit_turn_event``) and the agent-specific enum stream (ReAct produces it via ``ContentEmitter.emit``). But the **presentation projection** — turning those events into UI-consumable events and persisting a replayable transcript — existed only in the example: ``bot/webui/events.py`` (the ServerEvent family + DeltaEnvelope), the bot-layer projection logic (``BotTranscriptEmitter``'s hand-rolled state machine), and ``bot/webui/transcript_store.py`` (JSONL). Any second consumer wanting a UI today (editors beyond ACP, eval dashboards, a CLI console) had to rewrite the whole chain: turn identity assignment, segment buffering, tool-card pairing, transcript persistence and materialization.

## Decision

### 1. New framework package ``presentation`` (level 1)

It depends only on core/messaging/utils-level types (layering tree ``presentation: 1``, ledger empty). Four modules and one curated facade:

- **Event vocabulary** (``events.py``): a closed frozen discriminated union ``PresentationEvent`` (discriminator ``kind``) of 11 kinds covering the generic turn envelope:
  - Turn lifecycle: ``turn_started`` / ``turn_finished`` (carries ``StopReason`` + optional ``error`` + latency) / ``turn_errored`` (mid-turn observation). Interruption classification rides ``TurnFinished.stop_reason`` (single taxonomy); resume observation rides ``ApprovalResolved`` — ``TurnInterrupted`` / ``TurnResumed`` were deleted as dead kinds (ADR-0054);
  - Streaming deltas: ``text_delta`` / ``thinking_delta`` / ``tool_args_delta``;
  - Tool cards: ``tool_call_started`` (full arguments) / ``tool_result`` (full output + ``error``/``seq`` + the paired ``arguments``; ``arguments=None`` marks an orphan result with no call record);
  - Approval lifecycle: ``approval_requested`` / ``approval_resolved``;
  - Usage: ``usage_summary`` (``TokenUsage``);
  - Identity envelope: every event carries ``session_id``/``agent_name``/``turn_id`` (+ optional ``pool``/``workspace``/``timestamp_ms``).
  No bot-specific concepts (pool-affinity display, attachments, model-selection UI, WebUI block materialization) — those are enhancements layered on by consumers. Every kind has a default runtime producer on the core stream (ADR-0054's zero-producer anchor enforces it).
- **Projector contract** (``projector.py``): the ABC ``TurnEventProjector`` with a single method ``feed(event: TurnEvent) -> list[PresentationEvent]``. The input union is the CORE runtime union itself (ADR-0054) — it **imports no agent strategy package** (layer discipline). ``DefaultTurnEventProjector`` owns all state the projection needs: lazy turn identity (first content event assigns ``uuid4().hex[:12]``, emitting ``turn_started`` before content; ``turn_started`` assigns it eagerly), segment ids keyed by ``part_id``, pending tool-argument merging (the result card carries the paired full arguments; a result with no preceding call keeps ``arguments=None``), declared mapped/ignored disposition tables whose union equals the full core kind set (anchored), and turn-latency measurement via an injected clock.
- **Per-turn hub** (``hub.py``): ``SessionEventHub`` — a ``TurnEventSink`` that runs the default projector and fans each produced ``PresentationEvent`` out to registered ``PresentationSink`` consumers in registration order (delivery order is the observable contract). Turn identity comes from the hub's ``TurnBinding``: a binding ``turn_id`` is adopted as the projector's id, and a ``resumed=True`` binding (an approval resume carrying the suspended attempt's id) pre-activates the projector so the resumed leg continues the SAME turn with no second ``TurnStarted``. Deployment wiring keeps its own raw-core-stream sinks composed alongside the hub (``CompositeTurnEventSink``), while consumers that only need projected events register here.
- **Console renderer** (``console.py``): ``ConsolePresenter(PresentationSink)`` — the framework's reference terminal consumer (delta appends, one line per tool card, approval prompt line, turn terminator; injectable writer, no ANSI/curses). The second-consumer seam proof outside the example layer, demoed by ``examples/mini_project``.
- **Transcript contract** (``transcript.py``): the generic ABCs ``TranscriptStore[E]`` (append / load / load_sessions_by_prefix / list / delete / last_updated, keyed by full session id, prefix-merged) + ``TranscriptCodec[E]`` (records ↔ JSONL lines; ``event_time`` orders prefix merging; ``parse`` is the single legacy-conversion point — an old-format line adapts to zero-or-more records at read time) + the concrete base ``JsonlTranscriptStore[E]`` (``{safe_filename(sid)}.jsonl``, self-sufficient on stdlib + core). The framework ships ``PresentationTranscriptStore`` (``model_dump_json`` codec + ``load_turn_views``). Since ADR-0054's transcript cutover, presentation events ARE the durable transcript records (the L2 replayable transcript) — the bot's ServerEvent records survive only as read-time legacy conversions in its codec. Turn-view materialization: ``materialize_turns(events) -> list[TurnView]`` folds the delta stream back into ordered blocks (merging text/thinking segments, pairing tool cards, final stop reason) — the single transcript round-trip contract (the bot's parallel materializer is gone).

### 2. Explicit ignore-list discipline (no silent drops)

``DefaultTurnEventProjector`` declares the complete disposition table for the core ``TurnEvent`` kinds as two ClassVars: ``MAPPED_TURN_EVENT_KINDS`` and ``IGNORED_TURN_EVENT_KINDS`` (``iteration_started`` / ``iteration_finished``: intra-turn loop bookkeeping with no generic UI meaning; ``progress``: long-settlement heartbeat — consumers that want it observe the core stream directly). Their union must equal the full core kind set — the architecture anchor (T-P1) mechanically proves: a scripted real ReAct turn triggers every kind, and each kind either maps to ≥1 presentation event or is on the ignore-list — anything outside the two fails the test. (The pre-ADR-0054 form of this table keyed on the ReAct enum values; the runtime emission cutover moved the same discipline onto the core union, where it is anchored identically.)

### 3. Minimal sinking into the core seam

``core.turn_events.TurnToolResultEvent`` gains the optional fields ``error``/``seq``/``arguments`` (defaulting to None; old constructions unchanged): the tool-error fact, the ordering hint, and "the result carries its own call arguments" (an approval-resumed turn re-emits only END) are neutral facts — the presentation layer need not invent a second input record for them. ``safe_filename`` sinks from ``persistence/session_store.py`` to ``utils/file_io.py`` (the single filename mapping for all session/transcript stores keeps one implementation; persistence and the other callers now import from utils).

### 4. The bot converges into an implementation of this layer (no compatibility shim, byte-identical behavior)

- ``BotTranscriptEmitter`` no longer hand-rolls its state machine: turn identity, tool-argument pairing, and runtime-event mapping are owned by ``DefaultTurnEventProjector``; the base class is a ``TurnEventSink`` whose dispatch feeds a framework ``SessionEventHub`` (itself + a ``_ProjectionBridge`` consumer are the registered ``PresentationSink``s) and derives records + projections from the produced ``PresentationEvent`` (the pre-ADR-0054 method-channel entry points ``emit_delta``/``emit_turn_event``/``emit_complete``/``emit_error`` are gone — the sink face replaced them). Deleted from the bot: ``_ensure_turn_started`` (uuid assignment), ``_pending_external_tools`` (argument pairing), ``_turn_active``/``_turn_started_at`` bookkeeping. Segment buffering (aggregating by ``part_id`` into a single transcript event) and the WebUI ServerEvent projection stay bot-side — those are its transcript-format and wire-format implementation details. The ``_project_turn_end`` hook signature carries ``turn_id`` (the terminal identity is settled before the projector resets). Degenerate inputs (a tool end with no call_id) keep the bot-side direct-record path with the old wire bytes.
- ``bot/webui/transcript_store.py``: the bot's ``TranscriptStore`` ABC is deleted, replaced by a ServerEvent-parameterized specialization of the framework's generic ABC; ``JSONLTranscriptStore`` converges into the framework's ``JsonlTranscriptStore[ServerEvent]`` + ``ServerEventTranscriptCodec`` (``json.dumps(to_dict, ensure_ascii=False)``); file layout / prefix merging / deletion / mtime logic all sink into the framework, deleting ~120 lines of bot-side implementation. Since ADR-0054's transcript cutover the bot writes PRESENTATION records (the durable L2 transcript); the ServerEvent codec's ``parse`` is the single read-time legacy-conversion point for pre-cutover files, and replay materialization is the framework's single ``materialize_turns`` (the bot's ``MaterializedTurn`` folder is gone). ``WorkspaceRoutedTranscriptStore``/``ResilientTranscriptStore``/``SqliteTranscriptStore`` hang off the same contract (append's pool default changes from ``"main"`` to ``None`` → internal mapping; explicit callers' bytes unchanged).

### 5. Red anchors (behavior is machine-proven)

- ``tests/unit/presentation/test_default_projector_coverage.py`` (T-P1): a scripted **real** ``ReActAgent`` turn (four scenarios: streaming text, reasoning + argument stream + tool, iteration cap, provider crash) through a recording sink exercises every core ``TurnEvent`` kind → disposition completeness (mapped ∪ ignored == the full set, empty intersection), every mapped kind yields ≥1 presentation event, and after a JSONL round-trip ``materialize_turns`` reproduces the turn view (segment merging, tool-card pairing, terminal state); lazy identity / latency / argument-merging semantics are also pinned.
- ``tests/unit/presentation/test_plain_text_renderer.py`` (T-P2, the second-consumer proof): a ~45-line ``PlainTextTurnRenderer`` consumes **only** framework presentation types and renders a scripted turn end-to-end — mechanical proof that any new UI needs no bot code.
- The bot's full suite (~2750 tests) is the byte-compatibility acceptance net: the existing webui/transcript tests pass unchanged.

## Consequences

### Positive

- A second UI consumer = register a ``PresentationSink`` on a ``SessionEventHub`` and render ``PresentationEvent``/``TurnView`` (proven by T-P2 and the framework ``ConsolePresenter``) — no more copying the projection state machine, turn bookkeeping, or the transcript file machinery.
- Turn identity / argument pairing / ignore-list disposition have a single owner; the framework JSONL machinery and the bot's JSONL implementation converge into one (the codec is the single point of variation across record types).
- The presentation vocabulary is a frozen discriminated union, round-trip serialization safe (``model_dump_json`` ↔ ``validate_json``).

### Negative / recorded honestly

- The disposition table is declared by the projector's ClassVars and covered by the red anchor — consumers align with it rather than inventing their own. (The original cost — each consumer translating the ReAct enum stream by hand — died with ADR-0054's runtime emission cutover: both planes emit the core union at the source.)
- The bot's segment buffering / ServerEvent projection stay bot-side — the presentation vocabulary deliberately contains no "segment-completed record" class (the transcript record format is a consumer implementation detail).
- ``TurnToolResultEvent``'s three optional fields enter core (old constructions unchanged; serialization gains extra null keys — no byte-pinned test is affected, verified).

## Related

- ADR-0006 — core layering and dependency discipline (presentation follows the same sinking pattern)
- ADR-0051 — the package layering total-order tree (``presentation: 1``, ledger empty)
- ADR-0052 — runtime slotting (the prerequisite for converging the TurnEvent neutral seam)
