# model-multimodal-seam Specification

## Purpose
TBD - created by archiving change add-attachment-system. Update Purpose after archive.
## Requirements
### Requirement: ModelCapabilities is a carried, unused placeholder

`LLMConfig` SHALL declare a `ModelCapabilities` value object exposing a set of
`Modality` enum values. `Modality` SHALL include `TEXT` (always present), `IMAGE`,
`VIDEO`, and `AUDIO` (default-off), and SHALL be extensible. In this change, every
provider SHALL declare `TEXT` only; no other field SHALL be populated and nothing
SHALL read `ModelCapabilities` to alter runtime behavior. It exists solely as a
stable switch for the deferred native-multimodal renderer.

#### Scenario: default config carries TEXT only
- **WHEN** an `LLMConfig` is created with defaults
- **THEN** its `ModelCapabilities` SHALL contain only `TEXT`

#### Scenario: the placeholder does not change v1 behavior
- **WHEN** an attachment is processed in v1
- **THEN** it SHALL reach the agent as a path reference regardless of `ModelCapabilities`, because no non-TEXT modality is enabled

### Requirement: The native-multimodal renderer contract is documented but not implemented

The provider-side renderer contract for native multimodal (mechanism A) SHALL be
documented: when a `Modality` is on, attachments of that `kind` that pass the
perception gate SHALL render into model content blocks (image → `image_url`,
document → extracted text), each carrying its source path; on model rejection, the
block SHALL strip back to a text placeholder. This renderer SHALL remain dormant in
this change; a `TODO` SHALL mark the seam. Mechanism A SHALL NOT be implemented in
this change.

#### Scenario: the seam is marked but inactive
- **WHEN** the codebase is inspected after this change
- **THEN** a `TODO`-marked provider-side renderer seam SHALL exist and SHALL be dormant (no non-TEXT modality activates it)

### Requirement: Multimodal memory discipline is documented

The memory discipline for mechanism A SHALL be documented: multimodal blocks are
transient (stripped to a text placeholder before the message is persisted; restored
for the current turn only; never re-inlined for past turns); the transcript is the
durable index; memory compression never sees bytes. This discipline SHALL NOT be
implemented in this change — it is part of the deferred mechanism-A spec.

#### Scenario: the discipline is recorded as design, not code
- **WHEN** the change is complete
- **THEN** the strip/restore memory discipline SHALL appear as documented design (ADR + design.md), and no strip/restore code path for inline blocks SHALL be active

