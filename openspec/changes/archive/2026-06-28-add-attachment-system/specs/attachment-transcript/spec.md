## ADDED Requirements

### Requirement: The transcript is the attachment id→path index

Each Attachment SHALL be recorded with `id`, `kind`, `name`, `mime`, `size`, `path`,
and `locator` in the WebUI ServerEvent transcript. Download SHALL resolve an id by
scanning that session's transcript events. No separate attachment database SHALL
exist.

#### Scenario: download resolves an id by scanning the transcript
- **WHEN** a download is requested for `attachment_id` in session `s`
- **THEN** the system SHALL find the Attachment record by scanning session `s`'s transcript events and SHALL NOT consult any separate attachment store

### Requirement: Inbound and outbound records attach to the right event

Inbound Attachment records SHALL be carried in the user-message event; outbound
Attachment records SHALL be carried in the assistant-turn event of the ServerEvent
transcript.

#### Scenario: inbound upload records on the user message
- **WHEN** a user uploads a file
- **THEN** the Attachment record SHALL appear in the user-message ServerEvent event for that session

#### Scenario: outbound agent file records on the assistant turn
- **WHEN** the agent sends a file to the user
- **THEN** the Attachment record SHALL appear in the assistant-turn ServerEvent event for that session

### Requirement: Attachment records are immune to session compression

Attachment records SHALL live in the append-only ServerEvent transcript, not the
agent's LLM message history. The agent LLM history SHALL carry only ephemeral text
references. Session compression (which prunes the agent LLM history) SHALL NOT
remove or orphan Attachment records.

#### Scenario: compression does not drop an attachment record
- **WHEN** the agent LLM history for a session is compressed
- **THEN** every Attachment record in that session's ServerEvent transcript SHALL remain intact and downloadable

#### Scenario: base64 never enters stored transcript history
- **WHEN** a multimodal-capable turn renders an image inline (mechanism A, deferred)
- **THEN** the persisted record SHALL contain only a text reference or placeholder, never image bytes
