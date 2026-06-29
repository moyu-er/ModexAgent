# Native multimodal (mechanism A): capability registry, current-turn inline, placeholder-on-disk

Status: accepted

ADR-0013 shipped the attachment system with **mechanism B only** — every
attachment reaches the agent as a text path reference it inspects with tools.
It paved mechanism A (native multimodal: bytes inlined as model content blocks)
as a **dormant seam** and explicitly deferred it to "a future, separate spec"
(0013 §10a), naming two concrete blockers: a **provider gap** (the framework did
not pass multimodal content blocks to any LLM API) and **no activation signal**
(no per-model capability attribute to bind the renderer to).

This ADR is that separate spec. It decides how mechanism A is **activated**:
where model capabilities are declared, how the renderer is gated, and — the
load-bearing decision — how a multimodal block reaches the model on the turn it
matters **without ever being persisted as bytes**. It builds on the 0013 contract
without redesigning the attachment domain, the perception gate, the transcript
index, or mechanism B.

The investigation that grounded this decision examined the dormant seams already
in the framework (the `MediaProcessor` / `ImageHandler` renderer, the
`ModelCapabilities` / `Modality` placeholder, the dead `restore_multimodal_in_history`
helper, the key-only `_sanitize_api_messages`). The conclusion: the framework's
plumbing already tolerates
multimodal content end-to-end (`ChatMessage.content: str | list[dict] | None`,
both providers pass `list[dict]` content through to the API); the only real gap
is the activation switch and the memory discipline. Those are what this ADR pins
down.

## Decision

### 1. The activation signal is a declarative `ModelCapabilityRegistry`, not runtime inference

The capability a model has for a modality is **declared, not discovered at call
time**. A new deep module `ModelCapabilityRegistry` answers one question:
`capabilities_for(model) -> ModelCapabilities`. Everything that needs to decide
"do we inline this attachment as a vision block?" reads this.

Why declarative over the alternative (try the image, strip on provider error):
**the decision to inline must happen before the call**, not after a failure. The
whole point of mechanism A is to *choose* the inline form when the model can use
it and the mechanism-B path reference when it cannot. A purely reactive scheme
cannot make that choice — it can only react to a failure it caused, paying a
failing call every time a non-vision model receives an image. That is tolerable
as a **safety net** (§2) but not as the primary signal.

The registry has **two sources, merged in precedence order**:

1. **Framework bundled well-known defaults.** A maintained table mapping common,
   publicly-documented model identifiers to their supported `Modality` set. This is
   generic knowledge about external models, not business logic, so it lives in the
   framework and ships out-of-the-box.
2. **Business override.** A pool may declare capabilities explicitly, which
   **overrides the registry entirely** for that pool (the operator takes
   responsibility).

A registry **miss** (model not in the well-known table and not overridden)
resolves to **`TEXT` only** and logs a warning, so the operator knows to declare
it. This is the safe default: an undeclared model never receives an inline block
it may not understand; the attachment degrades to mechanism B.

**Name matching** is exact first, then family-prefix wildcard (so a model and its
point-releases share one entry), then the miss fallback above.

### 2. `LLMConfig.capabilities` becomes `None`-derives-from-registry

`LLMConfig.capabilities` today defaults to a frozen `TEXT`-only
`ModelCapabilities` and is never read. That default makes it impossible to tell
"the operator wants TEXT-only" from "the operator did not fill this in, please
look it up" — both are `TEXT`.

The field changes to `capabilities: ModelCapabilities | None = None`:

- `None` (the new default) → `create_llm_provider` resolves it by calling
  `ModelCapabilityRegistry.capabilities_for(config.model)`. This is the normal
  path.
- an explicit `ModelCapabilities(...)` → used verbatim, overriding the registry
  for that pool (the business override of §1).

`None` is the single "derive me" signal. This is the only config-shape change
mechanism A requires; the existing per-pool YAML + `${VAR}` env-reference
machinery is unchanged (model selection is already per-pool, not a single env
file).

### 3. Reactive sticky downgrade is the safety net, with a narrow trigger

Declarations can be wrong (a model is flagged as supporting a modality it does
not, or a provider rejects a specific content shape). The safety net: when the
provider returns a **non-transient error whose signal points at content-type /
modality rejection**, the provider **strips the inline blocks from that call and
retries once** so the turn survives, then **downgrades the capability on the
provider instance** (turns that `Modality` off in memory) so **subsequent turns
stop inlining that modality** and fall back to mechanism B.

The trigger is **narrow-matched**, not "any non-transient error": only an error
whose shape/message indicates modality or content-type rejection causes the
downgrade. A generic non-transient error (a real bug, a malformed request) is
raised as normal — silently stripping on every error would mask genuine
failures.

The downgrade is **process / pool lifetime** (the provider instance holds the
corrected capability until restart). It is **not persisted**: on restart the
registry is re-read, so the operator can correct a wrong entry. Persisting a
discovered downgrade risks a transient mis-flag permanently mislabelling a good
model, and adds state for no benefit — the registry is the source of truth, and a
restart re-establishes it.

This is the one place the framework learns about a model reactively. It is a
**correction** to the registry's answer, never the primary answer.

### 4. Activation gate: capability supports **and** a handler exists; otherwise mechanism B

For a gate-accepted attachment (it already passed the 0013 perception gate —
type + magic-byte MIME + per-kind size), *whether* it inlines depends on two
conditions, both required:

1. the resolved `ModelCapabilities` supports the attachment's `kind`'s modality
   (image → `IMAGE`, …), and
2. a renderer handler exists for that `kind`.

If both hold, the renderer inlines the attachment as a model content block. If
either fails, the attachment reaches the agent as the **mechanism-B path
reference** — exactly as it does today. Mechanism B is therefore the **floor**
for every attachment; mechanism A is an upgrade on top when capability and a
renderer allow it.

**First activation ships `IMAGE` only.** The only existing renderer handler is
the image handler (it already produces the OpenAI `image_url` data-URL block with
a `_meta.path`, which is the canonical internal shape every provider adapter
converts from). `VIDEO` and `AUDIO` remain declared in the `Modality` enum and
may be declared in the registry, but with no renderer handler they fail
condition (2) and fall through to mechanism B. Adding a modality later means
adding a handler — the gate and the branching do not change.

### 5. One degradation form — the placeholder *is* the mechanism-B reference

> From 0013 §10 (the dormant-renderer contract), preserved verbatim as the
> contract this ADR implements: *"On model rejection the renderer strips the
> inline block back to a text placeholder `[image: <path>]` … a seamless
> degradation to the mechanism-B form."*

This ADR sharpens "the mechanism-B form" into a literal rule: **the placeholder
an inline block degrades to is exactly the mechanism-B path-reference line**
`[Attachment: name (mime, size) @ absolute_path]` — the same string
`turn_context_builder.preprocess` already produces for non-inlined attachments.
There is **one** string and **one** degradation path:

- an attachment the model cannot inline (capability off, no handler, or
  post-downgrade) → this line, as live content;
- an attachment that *was* inlined this turn, when persisted to history → the
  block is replaced by **this same line**.

So "the model saw an image last turn but cannot re-see it" and "the model never
got the image" look **identical** in history: a path reference carrying
name/mime/size/path that the agent can still act on with tools. One generator
function (`_attachment_reference`) serves mechanism A's placeholder, mechanism
B's live text, and the persisted form.

### 6. Disk and the live LLM view diverge for the current turn only (load-bearing)

This is the decision that makes mechanism A safe, and it is forced by the
persistence architecture. Today a user message is written to `messages.jsonl` and
read back for the LLM from the **same** `ScopedMessageHistory` store — one
`append` is both the disk write and the source the LLM reads. With that identity,
the three possibilities are:

- disk = live = the inline block → base64 is persisted to history (violates "the
  transcript / memory never stores bytes"; re-sending all historical images every
  turn is token-prohibitive);
- disk = live = the placeholder → the model never sees the image even on the
  turn it arrived, so mechanism A does nothing;
- **disk = placeholder, live LLM view (current turn) = block** → the only viable
  design.

So the store and the live view **must diverge**, and only for the current user
turn:

- **At `append` time**, the message written to `messages.jsonl` carries the
  **placeholder form** (mechanism-B path references for every attachment — §5).
  This is what compression, pruning, and every future turn read.
- **For the current turn only**, the renderer restores the inline blocks into the
  **in-memory view** the LLM reads (`AgentContext.to_messages` →
  `history.to_list`), **without writing them back to disk**. The existing
  `restore_multimodal_in_history` helper (dead today) becomes this restore: it
  populates the in-memory cache the read path consults and deliberately does
  **not** trigger a disk rewrite.

Consequences of the divergence, all intended:

- **Current-turn only.** On the next turn `to_list` re-reads disk (placeholder),
  so past images are never re-inlined. The model sees live pixels for the file
  under discussion and cheap text markers for past files.
- **Compression-safe.** Pruning/summarization operates on the on-disk text
  (placeholders), never on bytes; if a placeholder is compacted away the
  attachment is not lost — its record remains in the append-only ServerEvent
  transcript (0013 §11).
- **Restart-safe.** A current-turn block lives only in process memory; a restart
  re-reads disk (placeholder). At worst the turn is reprocessed — consistent with
  "current turn only."

### 7. Provider layer: no passthrough change; one downgrade hook

The investigation confirmed the provider layer needs **no change to pass
multimodal content through**: `_sanitize_api_messages` filters message **keys**
only and never touches `content`'s value, so a `list[dict]` content array flows
unchanged into both providers' request payloads. The internal canonical shape is
the OpenAI Chat-Completions content block; provider adapters (or the routing
library, for non-OpenAI backends) handle any native conversion.

The only provider-layer addition is the **sticky-downgrade hook** of §3, attached
to the existing retry loop (`_execute_with_retry`): on a narrowly-matched
modality-rejection error, strip blocks, retry once, and turn the modality off on
the provider instance.

## Framework / business split

- **Framework** (`modex_agent/`): `ModelCapabilityRegistry` (the deep module +
  the bundled well-known defaults table); the `LLMConfig.capabilities = None →
  derive` resolution in `create_llm_provider`; the activation gate in
  `turn_context_builder.preprocess` (inline vs mechanism-B branching) and the
  renderer (`MediaProcessor` / handlers) it drives; the disk/placeholder strip at
  `append` and the in-memory-only `restore_multimodal_in_history`; the
  provider-layer sticky-downgrade hook.
- **Business** (`bot/`): per-pool capability overrides in pool YAML; declaring
  any project-used model the framework's well-known table does not yet cover.

## Considered options

- **Capability source — declarative registry vs reactive strip-on-error vs
  per-pool manual.** Chose declarative registry + reactive as safety net (§1, §3).
  Rejected pure-reactive: cannot choose the inline form *before* the call, pays a
  failing call per non-vision image. Rejected pure per-pool manual: redeclares
  the same model in every pool, error-prone; the registry centralizes it.
- **First-batch modality scope.** Chose `IMAGE` only (§4). The image handler is
  the only one that exists, so image is the only modality that can inline today.
  `VIDEO`/`AUDIO` stay declared but handlerless → mechanism B.
- **Placeholder form — the full mechanism-B line vs a short `[image: path]`.**
  Chose the full line (§5) so there is one degradation path and one string
  generator; a shorter placeholder loses name/mime/size and creates two forms.
- **Disk vs live divergence.** Not a real choice — the other two options violate
  either "memory never stores bytes" or mechanism A's purpose (§6). Recorded so a
  future reader does not "simplify" the divergence away and reintroduce either
  failure.

## Consequences

- Until a model is declared (in the well-known table or a business override) as
  supporting a modality, attachments of that modality reach the agent only as
  mechanism-B path references — the agent uses tools to inspect them, exactly as
  in 0013's v1. Activating a modality is: declare it, ensure a handler exists.
- A multimodal block is **transient**: persisted as a text placeholder, restored
  to a real block for the current turn only, never re-sent for past turns. Memory
  and compression never see bytes; the durable attachment is the transcript
  record (0013 §11), so compaction cannot lose it.
- `LLMConfig.capabilities` gains a real reader and a `None`-derives semantic;
  the field is no longer a dormant placeholder.
- The provider instance may correct its own capability at runtime (sticky
  downgrade), process-local and non-persistent; the registry remains the source
  of truth across restarts.
- Mechanism B is permanent: it is the floor for every attachment and the
  degradation target for mechanism A. Speciality recognition, oversized images,
  and any model without the modality all keep using the tool path.
- This ADR activates what 0013 §10/§10a paved and deferred. 0013's contract
  (perception gate, transcript index, asymmetric storage, mechanism B) is
  unchanged; 0013 §10a's "do not implement in the current plan set" is now
  satisfied by this ADR.
