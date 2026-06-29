## Context

ADR-0013 shipped attachments as **mechanism B**: every gate-accepted attachment
reaches the agent as a text reference (`[Attachment: name (mime, size) @ path]`)
that the agent inspects with tools. It paved **mechanism A** (native multimodal:
image bytes inlined as model content blocks) as a dormant seam and deferred it.
The dormant pieces already present: `ModelCapabilities` / `Modality` value
objects (unread), `ImageHandler` (produces the OpenAI `image_url` data-URL
block, zero live callers), `ChatMessage.content: str | list[dict] | None`, and
provider passthrough (`_sanitize_api_messages` filters keys only, never touches
`content`'s value).

The production message history is `ScopedMessageHistory`: it invalidates its
in-memory cache on every `append` and rebuilds the message list from
`messages.jsonl` on the next `to_list()`, reconstructing fresh `ChatMessage`
objects from serialized JSON. This round-trip is the central constraint the
design must respect.

ADR-0014 (revised this change) is the authoritative design record; this doc
summarizes the decisions and traces them to the spec requirements.

## Goals / Non-Goals

**Goals:**
- A pool whose model declares `IMAGE` receives image attachments as inline
  `image_url` content blocks on the turn they arrive.
- Image bytes **never** enter `messages.jsonl` (or any snapshot) — the core
  invariant.
- Inlining is current-turn-only: past turns see only text references.
- Default (`[text]`) preserves ADR-0013 v1 behavior; opt-in per pool.
- Minimal, framework/business-clean change reusing existing renderers.

**Non-Goals:**
- The framework `ModelCapabilityRegistry` / well-known model table and the
  `None`-derives config semantic.
- The reactive sticky-downgrade safety net (strip-and-retry on provider
  modality rejection).
- `VIDEO` / `AUDIO` inline renderers (declared modalities, no handler →
  mechanism B).
- First-iteration-only cadence (see D5 — every-iteration is the default).

## Decisions

### D1 — The carrier is turn state, not a `ChatMessage` field and not memory
**Decision:** the current turn's resolved attachments (path/mime/kind, no
base64) and the lazily-cached base64 live in `runtime.state.custom` under a new
`TurnCustomKey`, set when the turn's runtime/state is constructed
(`TurnContextBuilder.build_runtime_and_context`, where `ReActTurnState` is
built) by threading `input_msg.attachments_resolved` in, and read by the
enrichment. `preprocess` cannot host this write — it returns a
`(content, media_blocks, processor)` tuple and has no turn-state handle; the
turn state is built later, in `build_runtime_and_context`.

**Why over the alternatives:**
- A `ChatMessage` field with `Field(exclude=True)` is **defeated by the disk
  round-trip**: the excluded field is absent from `messages.jsonl`, so the next
  `to_list()` rebuilds the message without it — it is gone before the LLM reads
  it. (It survives in unit tests that use the in-memory `ListMessageHistory`,
  which is exactly the test-bypasses-a-layer trap.) A serialized field leaks
  bytes to disk. There is no middle ground on a disk-backed history that
  reconstructs objects from serialized form.
- Turn state is not the message store (untouched by `to_dict`/`save_messages`),
  is turn-local (resets each turn → "current-turn only" is structural), and
  survives the round-trip because it is read from the runtime, not rebuilt from
  JSON.
- Satisfies spec requirement "Image bytes never enter the message history"
  (carrier scenario).

### D2 — Persist vs. call are two strictly separated transforms
**Decision:** the persist action (preprocess) writes the user message as a
**string** = user text + mechanism-B references (unchanged from v1). The call
action (enrichment, each iteration) converts that message's content to a
multimodal `list[dict]` for the provider only, never writing back.

**Why:** the two must diverge precisely because of image capability; conflating
them is what made the original disk/live-divergence approach unviable. Keeping
persist unchanged means zero risk to history, compression, and future-turn
reads.

### D3 — A dedicated enrichment step after governance, not a governance subclass
**Decision:** the enrichment is one new step in `LLMNode._build_messages`,
invoked immediately AFTER `governance.apply(messages)`, reading
`ctx.runtime.model_capabilities` and turn state.

**Why over a governance subclass:**
- **Ordering is forced** — `TokenBudgetGovernance` / `LossyContentCompaction`
  must run on text before base64 exists, or a multi-MB image blows the budget /
  defeats compaction. The enrichment must be after the whole chain; a subclass
  inside the chain cannot cleanly guarantee "always last."
- **Governance's contract** `apply(messages) -> messages` carries no ctx,
  capability, or attachments; widening the ABC touches every subclass and
  caller.
- **Conceptual fit** — governance reduces context; enrichment materializes
  content. Separate domain-named modules (architecture rule 8).
- Satisfies spec requirement "Image attachments inline as content blocks"
  (isolation scenario).

### D4 — Front-verbatim + tail caption+url injection form
**Decision:** the call-time content is `[one text part = persisted string
verbatim] + [tail: per supported image attachment, `<image: name>` caption +
image_url block]`. The enrichment reuses the persisted string (no rebuild, no
parsing); it only appends the tail.

**Why over interleaved (reference-then-image per attachment):** interleaving
requires splitting the persisted string into per-attachment parts each
iteration (fragile parsing). Keeping the string verbatim preserves the existing
mechanism-B style exactly and needs no parsing, while the `<image: name>`
caption still maps each url to its reference. Non-image attachments appear only
in the front references (no tail block) — mechanism B stays the floor.

### D5 — Every-iteration injection with cached base64
**Decision:** base64 is computed once on first enrichment (read file by path),
cached in turn state, and reused on every iteration of the turn; the model sees
the image on every tool-loop iteration.

**Why over first-iteration-only:** the model may need the image alongside later
tool results, and the turn-state cache makes re-sending cheap. (This matches
the surveyed reference implementation. A first-iteration-only variant is a
trivial future toggle — clear the cache after first use — but is not the
default.)

### D6 — Per-pool YAML capability declaration; registry and downgrade deferred
**Decision:** `LLMConfig.capabilities` is read from `llm.capabilities` in the
pool YAML (flat list, default `[text]`). No `ModelCapabilityRegistry`, no
`None`-derives, no reactive downgrade in v1.

**Why over the full ADR-0014 §1/§3 machinery now:** declaration is the single
source of truth and is sufficient to gate inlining; the registry and downgrade
are pure additions layered on the same `ModelCapabilities` value object and
activation gate, so deferring them costs no rework. Matches the simplicity-first
constraint. Satisfies spec requirement "ModelCapabilities is read from per-pool
config."

### D7 — Capability on `AgentRuntimeServices`, exposed via `ctx.runtime.model_capabilities`
**Decision:** the resolved `ModelCapabilities` is set on
`AgentRuntimeServices` at pool build and exposed via `ctx.runtime.model_capabilities`,
parallel to `ctx.runtime.governance`. Governance never reads or writes it.

**Why:** capability is a stable per-pool fact; `AgentRuntimeServices` is
per-pool, stable across turns, and not serialized into snapshots. The parallel
access pattern keeps the two concerns isolated and the enrichment's dependency
explicit.

## Risks / Trade-offs

- **Snapshot may transiently hold cached base64** (an approval-interrupted
  turn's snapshot) → acceptable: it is a turn-local resume aid, not memory
  (`messages.jsonl` stays clean). If undesirable, move the cache to
  process-local `AgentRuntimeServices` keyed by turn UUID (out of the snapshot).
- **Every-iteration re-sends the image** → bounded by the turn's iteration
  count; mitigated by the cache (no re-encoding) and the per-kind size cap
  (ADR-0013 §7).
- **Mis-declared capability** (a pool declares `IMAGE` for a non-vision model)
  → provider rejects the block. v1 has no reactive downgrade; the operator
  corrects the YAML. (The deferred downgrade is the future mitigation.)
- **`<image: name>` caption relies on name matching** → names come from the
  same `Attachment` records as the references (shared helpers), so they cannot
  drift.

## Migration Plan

- Purely additive behind a default (`[text]`). No data migration; existing
  `messages.jsonl` is unaffected (it never held bytes).
- Rollback: remove `capabilities` from pool YAML (or set `[text]`) → reverts to
  mechanism B with no persisted artifact to clean.
- Remove the dead `restore_multimodal_in_history` helper and its call site
  (superseded; no live callers).

## Open Questions

- Cache location (turn state vs process-local services) — decided turn state for
  v1 (D5 risk note); revisit only if snapshot-byte presence proves objectionable.
- Caption wording `<image: name>` — acceptable for v1; can be tuned without
  structural change.
