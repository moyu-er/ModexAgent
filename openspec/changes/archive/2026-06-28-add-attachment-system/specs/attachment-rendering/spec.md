## ADDED Requirements

### Requirement: The WebUI renders a symmetric attachment card

`WebSocketOutputAdapter.send` SHALL render an attachment-card delta for any
Attachment, inbound or outbound, symmetrically. Image attachments SHALL render as
inline previews; non-image attachments SHALL render as file cards; a missing file
SHALL render as a fallback icon.

#### Scenario: image attachment renders inline
- **WHEN** an `image`-kind Attachment card is rendered in the WebUI
- **THEN** it SHALL render as an inline image preview referencing the download URL

#### Scenario: non-image attachment renders as a file card
- **WHEN** a non-image Attachment card is rendered
- **THEN** it SHALL render as a file card (name, size, download link)

#### Scenario: inbound and outbound render identically
- **WHEN** the same image is rendered once as inbound and once as outbound
- **THEN** both SHALL render the same way (direction-agnostic rendering)

### Requirement: A missing file degrades to a fallback icon

When the underlying file is gone (download returns 404), the rendered card SHALL
degrade to a fallback icon. This SHALL apply to both inbound and outbound.

#### Scenario: fallback icon for a missing file
- **WHEN** an Attachment's download returns 404
- **THEN** the WebUI SHALL render a fallback icon in place of the preview/card

### Requirement: The WebUI provides upload UI and uses shared limits for pre-validation

The frontend SHALL provide an upload UI. It SHALL fetch the active `MediaConfig`
limits from the backend and SHALL pre-validate selected files (size + extension)
before upload. Pre-validation SHALL be looser than the backend (no magic-byte
sniffing in the browser).

#### Scenario: the frontend blocks an oversized selection before upload
- **WHEN** the user selects a file exceeding the fetched image size cap
- **THEN** the frontend SHALL block the upload client-side and show a size message
