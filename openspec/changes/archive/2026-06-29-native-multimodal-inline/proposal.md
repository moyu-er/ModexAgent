## Why

ADR-0013 shipped attachments as **mechanism B only**: every attachment reaches
the agent as a text path reference it inspects with tools. It paved a dormant
native-multimodal seam (mechanism A) but explicitly deferred implementation.
Vision-capable models therefore never receive image pixels — every image
degrades to a path the agent must open with a tool, even when the model could
see it directly. This change activates mechanism A so a pool whose model
declares image capability receives image attachments as inline content blocks
on the turn they arrive, **without ever persisting image bytes to memory**.

## What Changes

- Activate the dormant multimodal seam: image attachments inline as `image_url`
  data-URL content blocks for pools whose model declares `IMAGE` capability.
- Add a dedicated call-time **enrichment step** in the ReAct LLM node that,
  after governance, converts the current turn's user message content into
  multimodal form: the persisted text-reference string verbatim as one text
  part, followed by a tail group of `<image: name>` caption + `image_url` block
  per supported image attachment. Non-image attachments stay as references only.
- Carry the current turn's resolved attachments and the lazily-cached base64 in
  **turn state** (`TurnCustomKey`) — transient, turn-local, never written to the
  message history. The image is present on every iteration of its turn and gone
  on the next turn.
- Make `LLMConfig.capabilities` readable from per-pool YAML
  (`llm.capabilities`, a flat list defaulting to `[text]`) and expose the
  resolved `ModelCapabilities` via `ctx.runtime.model_capabilities` (parallel to
  `ctx.runtime.governance`); governance never accesses it.
- Remove the dead `restore_multimodal_in_history` helper and its call site —
  superseded by the transient carrier (the original disk/live-divergence
  approach is unviable on the production disk-round-tripping history backend).
- **BREAKING**: none. Default `[text]` preserves ADR-0013 v1 behavior; image
  inlining is opt-in per pool.

**Out of scope (deferred):** the framework `ModelCapabilityRegistry` /
well-known model table, `LLMConfig.capabilities = None` derive-from-registry,
the reactive sticky-downgrade safety net, and `VIDEO`/`AUDIO` renderers. These
layer on the same `ModelCapabilities` value object and activation gate without
rework.

## Capabilities

### New Capabilities

_None._ This change activates an existing dormant OpenSpec capability rather
than introducing a new one. (`ModelCapabilities` below is the domain value
object that capability activates — the two senses of "capability" are distinct.)

### Modified Capabilities

- `model-multimodal-seam`: activate the dormant seam. All three of its existing
  requirements flip from "documented/dormant" to "implemented":
  - "ModelCapabilities is a carried, unused placeholder" → **rewritten**: it is
    now read from per-pool config (default TEXT-only) and gates image inlining.
  - "The native-multimodal renderer contract is documented but not implemented"
    → **rewritten**: implemented as a transient turn-state carrier plus a
    call-boundary enrichment step.
  - "Multimodal memory discipline is documented" → **rewritten**: implemented —
    image bytes never enter the message history; inlined for the current turn
    only.

## Impact

- **Framework** (`src/modex_agent/`):
  - `agents/react/nodes/llm.py` — new enrichment step in `_build_messages`,
    after `governance.apply`.
  - `pipeline/turn_context_builder.py` — populate the turn-state carrier at
    `preprocess` from `input_msg.attachments_resolved`.
  - `ioc/configs/llm.py` — `LLMConfig.capabilities` reader (flat list →
    `ModelCapabilities`).
  - `runtime/services.py` + `runtime/models.py`/`AgentRuntime` —
    `model_capabilities` on `AgentRuntimeServices`, exposed via
    `ctx.runtime.model_capabilities`; new `TurnCustomKey` in `runtime/enums.py`.
  - `utils/media_utils.py` — renderer: caption + `image_url` data-URL block.
  - `core/history.py` — remove `restore_multimodal_in_history`.
  - Pool build wiring (`ioc/factories`, business `pool_builder`) — resolve
    capabilities into services.
- **Business** (`examples/bot_project/`): `config/pools/*.yml` gains
  `llm.capabilities` (declare `[text, image]` for vision-capable pools).
- **Docs**: ADR-0014 revised to the transient-carrier design.
- **No provider passthrough change** (`_sanitize_api_messages` already passes
  `list[dict]` content through unchanged).
