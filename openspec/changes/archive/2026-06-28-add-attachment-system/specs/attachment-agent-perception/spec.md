## ADDED Requirements

### Requirement: The agent perceives attachments via a transient path reference

`turn_context_builder.preprocess` SHALL inject a path reference into the user message
for each accepted attachment, of the form
`[Attachment: <name> (<mime>, <size>) @ <absolute_path>]`. The injection SHALL be
transient: it SHALL enter the agent's LLM message history but SHALL NOT be persisted
into the transcript's user-message content. Session management (the transcript)
SHALL see the original user content plus the Attachment record; the agent memory
SHALL see the original content plus this injection.

#### Scenario: the agent receives a path reference for an uploaded file
- **WHEN** a user uploads an accepted attachment and the turn is built
- **THEN** the agent's user message SHALL contain `[Attachment: photo.png (image/png, 2.3MB) @ /abs/path/photo.png]`

#### Scenario: the transcript stores the original content, not the injection
- **WHEN** the same turn is persisted
- **THEN** the transcript user-message event SHALL store the original user content and the Attachment record, and SHALL NOT store the injected reference as part of the content

### Requirement: Injection is gated by the perception gate and carries no download id

The path reference SHALL be injected only for attachments that passed the perception
gate. The reference SHALL carry name, mime, size, and absolute path. It SHALL NOT
carry the download `attachment_id` (a frontend/download concern).

#### Scenario: a rejected attachment is not injected
- **WHEN** an attachment failed the perception gate at ingest
- **THEN** no path reference SHALL be injected for it (it was not stored)

#### Scenario: the reference omits the attachment id
- **WHEN** a path reference is injected
- **THEN** it SHALL contain the absolute path and SHALL NOT contain the `attachment_id`

### Requirement: The system provides no viewer; the agent selects tools

The attachment system SHALL NOT provide or force any file viewer. The agent SHALL
select which existing tool to invoke (filesystem, image-recognition MCP, document
tool) based on the referenced MIME. The system's only responsibility is awareness
plus a tool-usable path.

#### Scenario: the agent chooses a tool based on MIME
- **WHEN** the agent receives `[Attachment: x.png (image/png, ...) @ /p]`
- **THEN** the system SHALL NOT auto-process the image; the agent MAY invoke its image-recognition tool with `/p`
