# Tasks: native-multimodal-inline

> **Progress tracking**: After completing each task below and verifying it passes,
> invoke `change-progress native-multimodal-inline` to mark the checkbox before moving on.
> Do not batch-mark: complete one, mark one, then continue.

Flow: config/value-object (§1) → capability threading to the runtime (§2) →
turn-state carrier populated at preprocess (§3) → inline block renderer (§4) →
call-boundary enrichment that consumes §2+§3+§4 (§5) → cleanup + business
wiring + end-to-end (§6) → validate (§7). Each earlier group produces the
signal/state/helper the next group consumes; do not reorder.

## 1. Capability value object & config reader

- [ ] 1.1 Add a Pydantic `before` validator on `LLMConfig.capabilities` in `src/modex_agent/ioc/configs/llm.py` that coerces a flat `list[str]` (e.g. `[text, image]`) into `ModelCapabilities(modalities=frozenset(...))`; the default stays TEXT-only when the field is omitted. (`ModelCapabilities` stays a frozen `@dataclass`; the validator constructs it from the list input — do not convert it to a `BaseModel`.)
- [ ] 1.2 Add `tests/unit/ioc/test_llm_config_capabilities.py`: assert `[text, image]` parses to a `ModelCapabilities` containing `IMAGE`, omitted/`[text]` stays TEXT-only, and an unknown modality string is rejected.

## 2. Capability threading to the runtime

- [ ] 2.1 Add a `model_capabilities: ModelCapabilities | None` field to `AgentRuntimeServices` and a `model_capabilities` property on `AgentRuntime` (parallel to the existing `governance` property) in `src/modex_agent/runtime/services.py`.
- [ ] 2.2 At pool build, resolve `pool_cfg.llm.capabilities` into `AgentRuntimeServices.model_capabilities` (wiring in `src/modex_agent/ioc/factories/` and/or `examples/bot_project/bot/service/pool_builder.py`); add a test asserting `ctx.runtime.model_capabilities` reflects the declared pool config. Also add a grep-guard test asserting no `ContextGovernance` subclass references `model_capabilities` / `ctx.runtime.model_capabilities` (enforces the spec's "governance SHALL NOT read or write that field").

## 3. Turn-state carrier for the current turn's attachments

- [ ] 3.1 Add a `TurnCustomKey` member for the current turn's inline attachments (resolved `Attachment` records — path/mime/kind, no base64 — plus a base64 cache slot) in `src/modex_agent/runtime/enums.py`. The base64 cache is turn-local; per design a resume snapshot of an interrupted turn may transiently carry cached bytes (accepted — it is not the message history).
- [ ] 3.2 Populate that key in `src/modex_agent/pipeline/turn_context_builder.py::build_runtime_and_context` (where `ReActTurnState` is constructed, ~lines 428/475), threading the resolved image attachments from `input_msg.attachments_resolved` (add a param threaded from the caller — `preprocess` cannot host this; it has no turn-state handle); add a test asserting `agent_context.runtime.state.custom` carries the resolved image attachments after `build_runtime_and_context` and holds no base64 yet. (Hand-off: this is the carrier §5 reads.)

## 4. Inline block renderer (caption + image_url)

- [x] 4.1 Add `build_inline_image_block(att)` to `src/modex_agent/utils/media_utils.py` returning the tail pair `[{"type":"text","text":"<image: {name}>"}, {"type":"image_url","image_url":{"url":"data:{mime};base64,{b64}"}}]`, reading bytes lazily from `att.path` and reusing `ImageHandler`'s data-URL shape; add a unit test asserting the caption name matches `att.name` and the base64 round-trips the file.

## 5. Call-boundary enrichment step (load-bearing)

- [ ] 5.1 Write the divergence test FIRST (red) at `tests/unit/agents/react/test_multimodal_enrichment.py` using `ScopedMessageHistory`: (a) persisted user content is the text-reference string with NO base64/`image_url`; (b) the enriched current-turn content is a `list[dict]` = text part (verbatim string) + the caption+`image_url` tail produced by §4's `build_inline_image_block` per image; (c) on a later turn the past image is NOT inlined; (d) when a non-attachment user message follows the attachment message in the same turn, the image is attached to the current turn's user message, not to the later positional-last user message.
- [ ] 5.2 Implement the enrichment in `src/modex_agent/agents/react/nodes/llm.py::_build_messages`, invoked AFTER `governance.apply(messages)` and before `return`: target **the current turn's user message** (identified by turn identity — the message assembled for this turn — not merely the positional last user-role message), and when `ctx.runtime.model_capabilities` supports `IMAGE` and the turn-state carrier has image attachments, convert its content to `[{"type":"text","text": <persisted string verbatim>}] + tail(blocks from §4)`; compute base64 once and cache it in turn state, reuse on later iterations; otherwise leave content unchanged. Verify 5.1 goes green. (Consumes §2 capability, §3 carrier, §4 renderer.)
- [ ] 5.3 Add a guard test that `governance.apply` is unchanged and runs before enrichment (governance must not see base64): assert the enrichment step is positioned after the governance call in `_build_messages`.

## 6. Cleanup & business wiring

- [ ] 6.1 Remove the `restore_multimodal_in_history` helper from `src/modex_agent/core/history.py` and its call block in `src/modex_agent/pipeline/context_assembler.py` (the `if media_blocks and _media_processor is not None:` block at ~lines 99-106 that calls it). Note: this call is guarded dead today (`preprocess` always returns `[]`/`None` for media), but it would write image bytes to history if ever activated — our transient-carrier design supersedes it entirely. The pre-existing `media_blocks`/`_media_processor` plumbing in `preprocess`/`assemble_context` stays as a harmless no-op (out of scope to rip out).
- [ ] 6.2 Declare `llm.capabilities: [text, image]` in `examples/bot_project/config/pools/main.yml` (other pool YAMLs declare per their own model; left to operator discretion).
- [ ] 6.3 Extend `examples/bot_project/tests/integration/test_attachment_flow_to_llm.py`: assert a vision-capable pool's LLM-bound messages contain the `image_url` block on the arrival turn and only text references on a subsequent turn; assert a `[text]`-only pool never inlines.

## 7. Validation

- [ ] 7.1 Run `openspec validate native-multimodal-inline --type change --strict` to zero errors, then run the affected suites (`tests/unit/ioc/`, `tests/unit/agents/react/`, `tests/unit/utils/`, `examples/bot_project/tests/integration/test_attachment_flow_to_llm.py`) green.
