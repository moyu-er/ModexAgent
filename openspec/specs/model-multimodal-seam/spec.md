# model-multimodal-seam Specification

## Purpose
TBD - created by archiving change add-attachment-system. Update Purpose after archive.
## Requirements
### Requirement: ModelCapabilities is read from per-pool config and gates inlining

`LLMConfig.capabilities` SHALL be readable from per-pool YAML as a flat list of
`Modality` strings (for example `[text, image]`), defaulting to TEXT-only when
omitted. The resolved `ModelCapabilities` SHALL be exposed to the call-time
enrichment via `ctx.runtime.model_capabilities`. A pool that does not declare
`IMAGE` SHALL receive no inline image blocks, preserving ADR-0013 v1
mechanism-B behavior.

#### Scenario: omitted capability defaults to TEXT-only
- **WHEN** a pool config omits `llm.capabilities`
- **THEN** the resolved `ModelCapabilities` SHALL contain only `TEXT` and no attachment SHALL be inlined

#### Scenario: image capability declared and exposed
- **WHEN** a pool config declares `llm.capabilities: [text, image]`
- **THEN** the resolved `ModelCapabilities` SHALL include `IMAGE` and SHALL be reachable via `ctx.runtime.model_capabilities`

#### Scenario: capability is isolated from governance
- **WHEN** the enrichment step gates inlining
- **THEN** it SHALL read `ctx.runtime.model_capabilities`, and the governance chain SHALL NOT read or write that field

### Requirement: Image attachments inline as content blocks for vision-capable pools

The system SHALL inline image attachments as model content blocks for pools
whose model declares `IMAGE` capability. A dedicated enrichment step SHALL
convert the current turn's user message content to a multimodal `list[dict]`
after the governance chain runs and before the provider call: one text part
holding the persisted reference string verbatim, followed by a tail group
containing, for each image attachment whose modality is supported and has a
renderer, a caption text part (`<image: name>`) and an `image_url` data-URL
block. Non-image attachments SHALL appear only as references in the text part
and SHALL NOT receive an inline block. A pool without `IMAGE` SHALL fall back
to mechanism-B references only.

#### Scenario: vision pool inlines an image attachment
- **WHEN** the current turn's user message carries an image attachment and the pool supports `IMAGE`
- **THEN** the content sent to the provider SHALL be a `list[dict]` whose first element is the text part and whose tail contains an `<image: name>` caption followed by an `image_url` block for that attachment

#### Scenario: non-image attachment is not inlined
- **WHEN** the current turn's user message carries a text or document attachment
- **THEN** the content SHALL reference it only in the text part and SHALL NOT include any `image_url` block for it

#### Scenario: image is present on every iteration of its turn
- **WHEN** the turn executes multiple LLM iterations (a tool loop)
- **THEN** each iteration's enrichment SHALL include the image block, reusing cached base64 rather than re-encoding the file

#### Scenario: unsupported modality falls back to mechanism B
- **WHEN** an image attachment is processed in a pool that does not declare `IMAGE`
- **THEN** the content SHALL contain only the text reference and no `image_url` block

### Requirement: Image bytes never enter the message history

The system SHALL keep image bytes out of the persisted message history. The
persisted user message content SHALL be the text-reference string only; image
base64 SHALL NEVER be written to `messages.jsonl`. The current turn's resolved
attachments and the cached base64 SHALL be carried in turn state
(`TurnCustomKey`), which is turn-local and is not the message store. On any
turn after the image arrived, the image SHALL NOT be inlined. The enrichment
SHALL operate on the transient messages copy built for the provider and SHALL
NOT write back to history.

#### Scenario: persisted content carries no bytes
- **WHEN** a user message with an image attachment is appended to history
- **THEN** the persisted content SHALL be the text-reference string and SHALL NOT contain any base64 or `image_url` block

#### Scenario: the carrier is turn state, not the message
- **WHEN** the enrichment materializes image blocks
- **THEN** it SHALL read the current turn's attachments and cached base64 from turn state and SHALL NOT source them from the persisted message

#### Scenario: past turns do not re-inline
- **WHEN** a later turn reads history containing a previous turn's image-bearing user message
- **THEN** that message SHALL appear as the text-reference string only, with no `image_url` block

