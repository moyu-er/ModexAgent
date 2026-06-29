# Native multimodal (mechanism A): transient turn-state inline, persisted as text reference

Status: accepted (revised 2026-06-30)

ADR-0013 shipped the attachment system with **mechanism B only** — every
attachment reaches the agent as a text path reference it inspects with tools.
It paved mechanism A (native multimodal: image bytes inlined as model content
blocks) as a **dormant seam** and deferred it to "a future, separate spec"
(0013 §10a), naming two blockers: a **provider gap** (the framework did not
pass multimodal content blocks to any LLM API) and **no activation signal**
(no per-model capability attribute to bind the renderer to).

This ADR is that separate spec. It decides how mechanism A is **activated**:
where model capabilities are declared, how the inline renderer is gated, and —
the load-bearing decision — how an image reaches the model on the turn it
matters **without ever being persisted as bytes**. It builds on the 0013
contract without redesigning the attachment domain, the perception gate, the
transcript index, or mechanism B.

The original draft of this ADR (2026-06-29) proposed a **disk / live-view
divergence** for the current turn, implemented by reviving the dead
`restore_multimodal_in_history` helper. Re-reading the production persistence
path showed that approach is **unviable**: the production history backend
round-trips every message through disk on each read (see §6), so a
non-persisted field on the message is stripped before the LLM ever sees it,
while persisting it leaks bytes. This revision replaces that mechanism with a
**transient turn-state carrier plus a call-boundary enrichment step**, which
sidesteps the round-trip entirely: the image bytes never enter the message
history at all. The capability-source decision (§1) is also narrowed: the
framework `ModelCapabilityRegistry` and the reactive downgrade are **deferred**;
v1 declares capabilities per pool.

## Decision

### The core invariant

**Image bytes never enter the message history (`messages.jsonl`).** The bytes
live only in turn-local memory for the turn the image arrives, are injected
into the live LLM view on every iteration of that turn, and are gone on the
next turn. (A resume snapshot of an approval-interrupted turn may transiently
carry the cached bytes as a turn-local resume aid; that is not the message
history, and is the accepted trade-off documented in §3.) Everything below
exists to enforce this invariant while still letting a vision-capable model see
pixels on the turn that matters.

### 1. The activation signal is a per-pool capability declaration (registry deferred)

A model's modality support is **declared in the pool config**, not discovered
at call time. The existing `LLMConfig.capabilities` field (a frozen
`ModelCapabilities` defaulting to TEXT-only, previously unread) becomes the
signal: a pool declares the modalities its model accepts.

```yaml
# config/pools/main.yml
llm:
  model: "${LLM_MODEL:-...}"
  capabilities: [text, image]   # omitted → default [text]
```

A declaration of `[text, image]` enables image inlining for that pool; the
default `[text]` leaves every attachment on the mechanism-B tool path (exactly
ADR-0013 v1). `LLMConfig.capabilities` is read directly from config — no
`None`-derives indirection for v1.

**Deferred to a later spec.** The framework-bundled `ModelCapabilityRegistry`
(a well-known table mapping model identifiers to supported modalities, with
`LLMConfig.capabilities = None` → derive-from-registry) and the **reactive
sticky-downgrade** safety net (strip-and-retry on provider modality rejection,
turning the modality off for the pool's lifetime) are NOT built in v1.
Declaration is the single source of truth; a mis-declared model is corrected by
editing the pool config. These are pure additions layered on the same
`ModelCapabilities` value object and activation gate, so deferring them costs
no rework.

### 2. Two strictly separated actions — persist vs. call

Mechanism A turns on recognizing that **what is persisted and what the model
receives are two different transforms at two different points**, and they
diverge precisely because of image capability.

**Persist action** (turn start, `turn_context_builder.preprocess` — unchanged
from ADR-0013 v1): the user message `content` is a **string** = the user's text
plus one mechanism-B reference line per gate-accepted attachment
(`[Attachment: name (mime, size) @ absolute_path]`). This is the ONLY form ever
written to `messages.jsonl`. No bytes. Compression, pruning, and every future
turn read this string.

**Call action** (each LLM iteration, in `LLMNode._build_messages`, AFTER
`governance.apply`): for the current turn's user message only, a dedicated
**enrichment step** converts that message's `content` from the persisted string
into a multimodal `list[dict]`:

- a single text part holding the **persisted string verbatim** (no rebuild, no
  parsing) — the mechanism-B references stay in their original style; then
- a **tail group**: for each attachment whose modality is supported and has a
  renderer, a short caption text part (`<image: name>`) followed by an
  `image_url` data-URL block. Non-image attachments (text, docs) appear ONLY in
  the front references; they get no tail block — the agent inspects them with
  tools (mechanism B remains the floor).

The enrichment operates on the transient messages list built for the provider;
it never writes back to history.

### 3. The carrier is turn state, not the message and not memory

The current turn's resolved attachments (the `Attachment` records, carrying
`path`/`mime`/`kind`/`size` — **no base64**) are held in turn state
(`runtime.state.custom` under a new `TurnCustomKey`), set when the turn's
runtime/state is constructed — in `TurnContextBuilder.build_runtime_and_context`,
where `ReActTurnState` is created — by threading the resolved image attachments
from `input_msg.attachments_resolved`. (It is **not** set in `preprocess`,
which returns a `(content, media_blocks, processor)` tuple and has no
turn-state handle; the turn state is built later, in `build_runtime_and_context`.)

Turn state is the right carrier because:

- It is **not the message store**, so it is untouched by `to_dict()` /
  `save_messages` and never reaches `messages.jsonl`. (A field on `ChatMessage`
  cannot work — see §6.)
- It is **turn-local**: a fresh state object per turn, so the previous turn's
  attachments are naturally absent next turn. That is what makes "current-turn
  only" structural rather than enforced by cleanup.
- It **survives the disk round-trip** the production history performs on every
  read, because it is read from the runtime, not reconstructed from the
  serialized message.

The image bytes themselves are computed lazily and **cached in turn state** on
first enrichment (read file by `path`, base64-encode), then reused on every
later iteration of the same turn. The cache holds bytes only for the turn's
duration; it is not in the message store. (Snapshots of an
approval-interrupted turn may transiently carry the cached bytes; this is a
turn-local resume aid, not memory. If even that is undesirable, the cache can
live on process-local `AgentRuntimeServices` keyed by turn UUID — out of the
snapshot — at the cost of a little plumbing. v1 uses the turn-state cache.)

**Accepted v1 trade-off — approval resume.** `ReActSnapshotPolicy.state_from_snapshot`
does not restore `state.custom`, so an approval-interrupted turn, once resumed,
does NOT re-inline (its `INLINE_ATTACHMENTS`/`INLINE_IMAGE_CACHE` are absent);
the agent falls back to mechanism B (the tool path) for the remainder of that
resumed turn. Re-derivation on resume is a future improvement.

### 4. Activation gate: capability supports AND a handler exists; otherwise mechanism B

For a gate-accepted attachment (it already passed the 0013 perception gate),
*whether* it inlines depends on two conditions, both required:

1. the resolved `ModelCapabilities` supports the attachment's `kind`'s modality
   (image → `IMAGE`, …), and
2. a renderer handler exists for that `kind`.

If both hold, the enrichment appends the caption + `image_url` block for that
attachment. If either fails, the attachment appears only as the mechanism-B
reference line (in the front text part) — exactly as it does today. Mechanism B
is therefore the **floor** for every attachment; mechanism A is an upgrade on
top when capability and a renderer allow it.

**First activation ships `IMAGE` only.** The image renderer (`ImageHandler`)
already produces the OpenAI `image_url` data-URL block; that is the canonical
internal shape. `VIDEO` and `AUDIO` remain in the `Modality` enum and may be
declared, but with no renderer they fail condition (2) and fall through to
mechanism B. Adding a modality later means adding a handler — the gate and the
branching do not change.

### 5. One reference form, two roles

The mechanism-B reference line `[Attachment: name (mime, size) @ absolute_path]`
serves both roles and is generated by one function (`_attachment_reference`,
already present):

- **Persisted form** (front text part): the line, as live content, for every
  attachment — this is what history holds forever.
- **Call caption** (tail): the `<image: name>` caption reuses the same
  `Attachment.name` so the model can map each `image_url` block back to the
  reference that describes the file.

The persist path (preprocess) and the call path (enrichment) both derive from
the same `Attachment` records via shared helpers, so the names never drift. The
call path does NOT re-emit the references — it reuses the persisted string
verbatim as the front text part; it only appends the tail.

### 6. Why not the disk / live-view divergence (supersedes the original §6)

The original draft restored the inline block into the in-memory history view
for the current turn via `restore_multimodal_in_history`, leaving the disk copy
as the placeholder. **This does not work in production**, and the reason is
structural:

`ScopedMessageHistory` (the production backend) invalidates its in-memory cache
on every `append` and rebuilds the message list from disk (`messages.jsonl`)
on the next `to_list()` — reconstructing fresh `ChatMessage` objects from the
serialized JSON. So for a `ChatMessage` field:

- if the field is **excluded from serialization** (`Field(exclude=True)`), the
  disk copy never has it, and the next read rebuilds the message without it —
  the field is gone before the LLM sees it. (This passes in tests, which use
  the in-memory `ListMessageHistory` that returns the original objects, but
  fails silently in production — exactly the test-bypasses-a-layer trap.)
- if the field is **serialized**, its base64 is written to `messages.jsonl` —
  violating the core invariant.

There is no middle ground on a disk-backed history that reconstructs objects
from their serialized form. The transient **turn-state carrier** (§3) is the
escape: the bytes are never on the message, so the round-trip cannot strip or
leak them. This achieves the same observable behavior the original §6 wanted
(disk = reference, current-turn live view = block, gone next turn) without
touching the persistence path at all. `restore_multimodal_in_history` stays
dead and should be removed.

### 7. A dedicated enrichment step, not a governance

The call-time enrichment is a **dedicated step in `LLMNode._build_messages`**,
invoked immediately AFTER `governance.apply(messages)` and before the messages
go to the provider — NOT a `ContextGovernance` subclass. Four reasons:

1. **Ordering is forced.** Governance (`TokenBudgetGovernance`,
   `LossyContentCompactionGovernance`) must run on **text** before any base64
   is present, or a multi-megabyte image blows the token budget / defeats
   compaction. The enrichment therefore runs after the whole governance chain.
   A governance subclass inside the chain cannot cleanly guarantee "always
   last"; a step after `governance.apply` can.
2. **Governance's contract has no context.** `apply(messages) -> messages`
   carries no `AgentContext`, no capability, no attachments. Enrichment needs
   all three; widening the ABC touches every subclass and caller, against the
   "minimal change" goal.
3. **Conceptual mismatch.** Governance is context *treatment* (compaction,
   budget, repair — it reduces). Enrichment is content *materialization* (it
   adds bytes). Per the architecture rules they deserve separate, domain-named
   modules.
4. **Minimal change.** One new function plus one line in `_build_messages`; the
   governance ABC, its subclasses, and its callers are untouched.

### 8. Capability threading — parallel to governance, isolated from it

The resolved `ModelCapabilities` is attached to `AgentRuntimeServices`
(`services.model_capabilities`, set at pool build from `pool_cfg.llm.capabilities`)
and exposed via `ctx.runtime.model_capabilities` — the same access pattern as
`ctx.runtime.governance`. The enrichment reads `ctx.runtime.model_capabilities`
to gate inlining. **Governance never reads or writes this field**; the two are
sibling services accessed separately, keeping the concerns isolated. Because
`AgentRuntimeServices` is per-pool and stable across turns (and is not
serialized into snapshots), capability — a stable per-pool fact — fits it
exactly.

### 9. Provider layer needs no passthrough change

The investigation confirmed the provider layer passes multimodal content
through unchanged: `_sanitize_api_messages` filters message **keys** only and
never touches `content`'s value, so a `list[dict]` content array flows
unchanged into both providers' request payloads. `ChatMessage.content` is
already typed `str | list[dict] | None`. No provider change is required for v1;
the deferred reactive downgrade (§1) is the only provider-layer addition
envisioned, and only when that work is picked up.

## Framework / business split

- **Framework** (`modex_agent/`): the enrichment step in `LLMNode._build_messages`
  and the renderer it drives (`MediaProcessor` / `ImageHandler`); the turn-state
  carrier (`TurnCustomKey`) populated in `build_runtime_and_context`; the `ModelCapabilities` /
  `Modality` value objects; the `ctx.runtime.model_capabilities` exposure on
  `AgentRuntimeServices`; the per-pool `LLMConfig.capabilities` reader (flat
  list → `ModelCapabilities`).
- **Business** (`examples/bot_project/`): declaring `capabilities` in each
  pool's `llm:` YAML block for the models it actually uses.

## Considered options

- **Carrier — `ChatMessage` field vs turn state vs rebuild-from-string.** Chose
  turn state (§3, §6). A `ChatMessage` field is defeated by the production
  disk round-trip (excluded ⇒ stripped before the LLM reads; serialized ⇒
  leaked to disk). Rebuilding the call-time content by parsing the persisted
  string is fragile. Turn state survives the round-trip, is turn-local, and is
  not the message store.
- **Injection form — interleaved (reference then image, per attachment) vs
  front-verbatim + tail group.** Chose front-verbatim + tail group (§2, §5).
  Interleaving requires splitting the persisted string into per-attachment
  parts each iteration (parsing). Keeping the persisted string verbatim as one
  text part and appending a tail group of `[caption + image_url]` per image
  needs no parsing, preserves the existing mechanism-B style exactly, and still
  lets the caption (`<image: name>`) map each url to its reference.
- **Within-turn cadence — first-iteration-only vs every iteration.** Chose
  every iteration (§3): the model may need the image alongside later tool
  results, and the turn-state cache makes re-sending cheap. (A first-iteration-
  only variant is a trivial future toggle — clear the cache after the first
  enrichment — but is not the default.)
- **Enrichment seam — governance subclass vs dedicated step.** Chose dedicated
  step (§7) for ordering, contract, conceptual, and minimality reasons.
- **Capability source — registry vs per-pool declaration.** Chose per-pool
  declaration for v1 (§1); registry + reactive downgrade deferred as non-load-
  bearing additions.

## Consequences

- Until a pool declares a modality (and a renderer exists for it), attachments
  of that modality reach the agent only as mechanism-B path references — the
  agent inspects them with tools, exactly as in ADR-0013 v1.
- An inlined image is **transient**: persisted as a text reference, present as
  real pixels in the live view for every iteration of the turn it arrived, and
  absent on all later turns. Memory and compression never see bytes; the
  durable attachment record is the append-only ServerEvent transcript
  (ADR-0013 §11), so compaction cannot lose it.
- `LLMConfig.capabilities` gains a real reader and a per-pool YAML shape; the
  field is no longer a dormant placeholder.
- Mechanism B is permanent: it is the floor for every attachment and the
  degradation target. Non-image attachments, oversized images a pool chooses
  not to inline, and any model without the declared modality all keep the tool
  path.
- The dead `restore_multimodal_in_history` helper and its call site are removed
  (§6) — the transient carrier replaces them.
- This ADR activates what ADR-0013 §10/§10a paved and deferred. 0013's contract
  (perception gate, transcript index, asymmetric storage, mechanism B) is
  unchanged; 0013 §10a's "do not implement in the current plan set" is now
  satisfied by this ADR (v1 scope: per-pool declaration, IMAGE only, no
  registry, no reactive downgrade).
