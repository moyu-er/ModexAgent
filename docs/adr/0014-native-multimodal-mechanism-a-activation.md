# Native multimodal (mechanism A): transient inline + tool-side capability awareness

Status: accepted (revised 2026-07-29)

## History

- **Original draft (2026-06-29):** proposed a disk / live-view divergence for
  the current turn, implemented by reviving the dead
  `restore_multimodal_in_history` helper. Unviable — the production history
  backend round-trips every message through disk on each read, so a
  non-persisted field is stripped before the LLM sees it (§6).
- **First revision (2026-06-30):** replaced the divergence mechanism with a
  transient turn-state carrier plus a call-boundary enrichment step. Solved
  the **user-attachment side** — images attached by the user are inlined into
  the LLM call for the turn they arrive, never persisted as bytes.
- **This revision (2026-07-29):** fills the **tool side** gap the first
  revision left open. Tools can now declare modality needs declaratively
  (`required_modalities` / `produced_modalities`); tool-produced images are
  injected via a pluggable strategy (Path B — synthetic user message with
  per-call attribution); tool results follow the "observation, not system log"
  principle. The former ADR-0036 content is merged here.

## Context

ADR-0013 shipped the attachment system with **mechanism B only** — every
attachment reaches the agent as a text path reference it inspects with tools.
Mechanism A (native multimodal: image bytes inlined as model content blocks)
was a dormant seam.

Two sides needed activation:

1. **User-attachment side** (solved 2026-06-30): user-attached images are
   inlined into the LLM call when the active model supports `IMAGE`, never
   persisted as bytes.
2. **Tool side** (solved 2026-07-29): the multimodal `read` tool
   (`ReadFileTool`) can return an `image_url` content block, but had no way to
   *declare* that need to the framework, no caps-aware description, and no
   structured injection path for tool-produced images.

This ADR covers both. The core invariant — **image bytes never enter the
message history** — applies to both sources.

## Decision

### The core invariant

**Image bytes never enter the message history (`messages.jsonl`).** The bytes
live only in turn-local memory for the turn the image matters, are injected
into the live LLM view on every iteration of that turn, and are gone on the
next turn. Everything below exists to enforce this invariant while still
letting a vision-capable model see pixels on the turn that matters.

### 1. Capability source — per-pool declaration (registry deferred)

A model's modality support is **declared in the pool config**, not discovered
at call time. The `LLMConfig.capabilities` field (a frozen
`ModelCapabilities` defaulting to TEXT-only) is the signal.

```yaml
# config/pools/main.yml
llm:
  model: "${LLM_MODEL:-...}"
  capabilities: [text, image]   # omitted → default [text]
```

`Modality` / `ModelCapabilities` / `ModelInfo` live in `core/capabilities.py`
(moved from `ioc/configs/llm.py` so `core/tool_manager` and
`runtime/services` can reference them without upward ioc dependency).
`ModelInfo` is threaded through `AgentRuntimeServices.model_info` →
`AgentContext.runtime_info[RuntimeInfoKey.MODEL_INFO]` →
`ToolExecutionContext.model_info` (frozen BaseModel delivered via contextvar,
set by `ToolManager.execute`).

**Deferred:** the framework-bundled `ModelCapabilityRegistry` and the
reactive sticky-downgrade safety net are not built; declaration is the single
source of truth.

### 2. Two image sources, two injection paths, one shared gate

Both user attachments and tool-produced images are gated on
`ModelCapabilities.supports(Modality.IMAGE)`. Both use the same `image_url`
wire format. Both are transient — only the LLM call copy is mutated; persisted
history keeps text-only content. But they inject at **different positions**
because they have different semantics:

| Source | Injection position | Attribution |
|--------|-------------------|-------------|
| User attachment | INTO the last user message's content | None (user sent it) |
| Tool-produced image | NEW synthetic `role: "user"` message appended AFTER tool results | Per-call: `"Media from tool '{tool_name}' (call {call_id}):"` |

**User attachments** inject into the existing user message because the user
sent them — they belong on the user's message. The persisted string becomes
the first text part; caption + `image_url` blocks are appended as a tail.

**Tool-produced images** inject as a separate synthetic user message because
they originate from a tool call, not the user. Appending after tool results
preserves temporal ordering (image after the tool call that produced it, not
before). Per-call attribution lets the model map each image to its originating
tool call — essential when multiple `read` calls produce multiple images in one
turn. This is an improvement over opencode's Path B, which uses a generic
`"Attached media from tool result:"` label with no per-call attribution.

### 3. Tool-produced media injection strategy (Path B)

A `ToolResultMediaStrategy` ABC (`media/tool_media.py`) abstracts how
tool-produced media reaches the LLM:

- **`SyntheticUserMessageStrategy`** (Path B, default): appends a synthetic
  `role: "user"` message after tool results, carrying per-call attribution
  text + `image_url` blocks. Used by providers whose `role: "tool"` message is
  string-only (OpenAI Chat Completions, most OpenAI-compatible endpoints via
  LiteLLM).
- **Future `InToolResultStrategy`** (Path A): media stays inside the
  tool-result message's content array. Requires providers whose tool message
  accepts multimodal content (OpenAI Responses API, Anthropic `tool_result`).
  Not implemented; the ABC is the extension point.

`TOOL_MEDIA_CACHE` (per-turn, keyed by `call_id`) stores `ToolMediaEntry`
(frozen Pydantic: `call_id` + `tool_name` + `image_blocks`). `ToolNode`
populates it when a tool result has `content_blocks`; `enrich_inline_media`
reads it and delegates to the strategy.

`enrich_inline_media` (in `LLMNode._build_messages`, after governance) splits
into two paths: user attachments → `_inject_into_last_user_message`; tool
media → `strategy.inject_tool_media`. The two paths are fully separated — no
shared `all_blocks` list.

### 4. Declarative tool capability awareness

Tools declare modality needs as class attributes on `Tool`:

- **`required_modalities`** (default empty) — modalities the model MUST
  support for the tool to be *usable*. Drives the visibility filter: a tool
  whose required modalities are not met is **hidden** from the tool list.
- **`produced_modalities`** (default empty) — modalities the tool may
  *produce* in its output (e.g. `ReadFileTool.produced_modalities = {IMAGE}`).
  **Not** a visibility gate: a tool that produces images is still listed for a
  text-only model — it degrades at runtime. But the tool can adjust its
  description via `get_dynamic_schema_for(caps)` so the model knows the image
  path is unavailable.

`Tool.is_available(caps)` is the visibility gate. `ToolManager
.get_tool_descriptions(caps)` filters at **query time** (not register time —
caps are per-turn; `ModelChoiceBindHook` can switch models mid-conversation).

`ReadFileTool` and `ScopedReadFileTool` declare
`produced_modalities = frozenset({Modality.IMAGE})` and override
`get_dynamic_schema_for(caps)`: when `caps` lacks `IMAGE`, the description
sentence "Image files may also be read as visual content" is rewritten to
"Image files cannot be read as visual content by the current model."
`WorkspaceScopedTool` delegates `get_dynamic_schema_for` to its inner tool.

### 5. ToolResult.content — single source of truth

`ToolResult.content: list[ContentPart]` is the single source of truth (the
old `result: Any` field is removed; `extra="forbid"` locks the shape).
`image_blocks` and `content_blocks` are `@computed_field` (derived from
`content`'s `ImageUrlPart`s). `from_text()` classmethod for text-only results.
`message_content()` renders only `TextPart`s — so a multimodal ToolResult MUST
include a `TextPart` describing the image (e.g. `"[Image read: foo.png
(image/png)]"`), or the persisted tool message content is empty.

`ToolExecutionContext.supports(modality)` is the conservative runtime helper:
returns **False when `model_info is None`** (tools degrade gracefully).

### 6. Tool result as agent observation, not system log

When a tool cannot fulfill a request (e.g. image read on a text-only model),
the result states the **objective fact only** — no system diagnosis ("model
lacks IMAGE capability"), no file sizes, no action advice ("use a
vision-capable model"). The agent may have called the tool autonomously;
prescribing actions interrupts its reasoning. The capability limitation is
already surfaced via the tool description (`get_dynamic_schema_for` adjusts it
for text-only models). Example: `"Image file: /path (image/png). Visual
content not available."`

### 7. Persist vs. call — two strictly separated actions

**Persist action** (turn start): the user message `content` is a **string** =
the user's text plus one mechanism-B reference line per gate-accepted
attachment. This is the ONLY form ever written to `messages.jsonl`. Tool
results persist their `message_content()` text (TextParts only — ImageUrlParts
are stripped).

**Call action** (each LLM iteration, after `governance.apply`): for the
current turn's messages only, `enrich_inline_media` converts the relevant
message content from string into multimodal `list[dict]` — user attachments
into the last user message, tool images into a synthetic appended user
message. The enrichment operates on the transient messages list built for the
provider; it never writes back to history.

### 8. The carrier is turn state, not the message and not memory

The current turn's resolved attachments and tool media cache are held in turn
state (`runtime.state.custom` under `TurnCustomKey`), not on `ChatMessage` and
not in memory. Turn state is the right carrier because:

- It is **not the message store** — untouched by `to_dict()` / `save_messages`.
- It is **turn-local** — a fresh state per turn, so previous turn's images are
  naturally absent.
- It **survives the disk round-trip** — read from the runtime, not
  reconstructed from serialized messages.

**Accepted v1 trade-off — approval resume.** `ReActSnapshotPolicy
.state_from_snapshot` does not restore `state.custom`, so an
approval-interrupted turn does NOT re-inline after resume; the agent falls
back to mechanism B (the tool path).

### 9. Enrichment is a dedicated step, not a governance

The call-time enrichment runs in `LLMNode._build_messages`, immediately AFTER
`governance.apply(messages)` and before the provider call — NOT a
`ContextGovernance` subclass. Governance must run on **text** before any
base64 is present (or a multi-megabyte image blows the token budget). The
enrichment needs `AgentContext` + capability + attachments, which governance's
`apply(messages) -> messages` contract does not carry.

### 10. Provider layer needs no passthrough change

`_sanitize_api_messages` filters message **keys** only and never touches
`content`'s value, so a `list[dict]` content array flows unchanged into both
providers' request payloads. `ChatMessage.content` is already typed
`str | list[ContentPart] | None`.

## Framework / business split

- **Framework** (`modex_agent/`): `Modality` / `ModelCapabilities` /
  `ModelInfo` in `core/capabilities.py`; `ToolResultMediaStrategy` ABC +
  `SyntheticUserMessageStrategy` + `ToolMediaEntry` in `media/tool_media.py`;
  `enrich_inline_media` in `LLMNode`; `Tool.required_modalities` /
  `produced_modalities` / `is_available` / `get_dynamic_schema_for`;
  `ToolExecutionContext.supports`; caps-aware `get_tool_descriptions`;
  `ReadFileTool` / `ScopedReadFileTool` multimodal + caps-aware overrides;
  `WorkspaceScopedTool` delegation; `ToolResult.content` unification;
  `TOOL_MEDIA_CACHE` / `INLINE_ATTACHMENTS` / `INLINE_IMAGE_CACHE` turn-state
  carriers; `ModelInfoProvider` system-prompt injection.
- **Business** (`examples/bot_project/`): declaring `capabilities` per pool
  YAML; no framework changes needed.

## Considered options

- **Carrier — `ChatMessage` field vs turn state.** Chose turn state: a
  `ChatMessage` field is defeated by the production disk round-trip (excluded
  ⇒ stripped; serialized ⇒ leaked).
- **Injection form — interleaved vs front-verbatim + tail group.** Chose
  front-verbatim + tail: no parsing, preserves mechanism-B style, caption maps
  each url to its reference.
- **Tool media injection — into existing user message vs synthetic user
  message.** Chose synthetic user message (Path B): preserves temporal
  ordering (image after tool call), enables per-call attribution, separates
  tool-produced images from user-attached images.
- **Tool media strategy — Path A (in-tool-result) vs Path B (synthetic user
  message).** Chose Path B for v1: OpenAI Chat Completions `role: "tool"`
  accepts string content only. Path A requires Responses API or Anthropic
  native format; the strategy ABC is the extension point.
- **Register-time vs query-time capability filtering.** Chose query-time:
  capabilities are per-turn (model selector can switch mid-conversation);
  register-time would bind the tool list to whichever model was active first.
- **`produced_modalities` as visibility gate vs declarative metadata.** Chose
  declarative metadata: a tool that produces images is still useful to a
  text-only model (reads text, degrades image path). Hiding it would remove a
  working tool.
- **Enrichment seam — governance subclass vs dedicated step.** Chose dedicated
  step for ordering (after governance), contract (needs context), and
  conceptual (enrichment adds, governance reduces) reasons.
- **Capability source — registry vs per-pool declaration.** Chose per-pool
  declaration for v1; registry + reactive downgrade deferred.

## Consequences

- Until a pool declares a modality (and a renderer exists for it), attachments
  of that modality reach the agent only as mechanism-B path references.
- An inlined image — whether user-attached or tool-produced — is **transient**:
  persisted as text, present as pixels in the live view for the turn it
  matters, absent on all later turns.
- Tool-produced images are attributed per-call in a synthetic user message,
  so the model can distinguish them from user-attached images and map each to
  its originating tool call.
- A text-only model sees an adjusted description for tools that produce IMAGE
  ("cannot be read as visual content by the current model") rather than
  discovering the limitation via a failed call.
- Tool results state objective facts only — no system diagnosis or action
  advice — so the agent can decide how to proceed without being interrupted.
- `LLMConfig.capabilities` gains a real reader and a per-pool YAML shape.
- Mechanism B is permanent: it is the floor for every attachment and tool
  result, and the degradation target.
- Future audio/video tools add a `Modality` enum member and declare
  `required_modalities` / `produced_modalities` — the gate, filtering,
  description seam, and injection strategy do not change.
- The `ToolResultMediaStrategy` ABC allows a future `InToolResultStrategy`
  (Path A) when the provider layer switches to the Responses API, without
  changing callers.
- This ADR activates what ADR-0013 §10/§10a paved and deferred. 0013's
  contract (perception gate, transcript index, asymmetric storage, mechanism
  B) is unchanged.
