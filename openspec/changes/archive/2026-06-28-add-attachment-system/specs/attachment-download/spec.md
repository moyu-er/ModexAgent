## ADDED Requirements

### Requirement: Download uses an opaque capability id plus workspace routing

The download endpoint SHALL be
`GET /api/sessions/{session_id}/attachments/{attachment_id}?ws=<ws>`. The
`attachment_id` (uuid) SHALL be the capability — no HMAC signing, no auth round-trip.
The `?ws=` query parameter SHALL be routing only, resolved by the existing
single-source-of-truth workspace-root resolver (the same one every other WebUI HTTP
endpoint uses). No session→workspace reverse index SHALL be introduced.

#### Scenario: id alone is the access capability
- **WHEN** a client requests a valid `attachment_id` with the correct `?ws=`
- **THEN** the file SHALL be served without any additional signature or auth

#### Scenario: unknown id is not found
- **WHEN** a client requests an `attachment_id` not present in the session's transcript
- **THEN** the endpoint SHALL return 404

### Requirement: Download dispatches on locator

Download SHALL dispatch on the Attachment `locator`: `media` SHALL read via
`MediaStore.read`; `workspace` SHALL read the filesystem at the literal stored path.

#### Scenario: media-locator file read from MediaStore
- **WHEN** an inbound (`locator=media`) attachment is downloaded
- **THEN** the bytes SHALL be served from `MediaStore.read`

#### Scenario: workspace-locator file read from the filesystem
- **WHEN** an outbound (`locator=workspace`) attachment is downloaded
- **THEN** the bytes SHALL be served from the stored absolute filesystem path, not copied

### Requirement: Serving applies a MIME allow-list

Only `image/*` and `video/*` SHALL be served with their real `Content-Type`; every
other file SHALL be served as `application/octet-stream` so a browser cannot sniff
executable content. SVG responses SHALL carry a strict `Content-Security-Policy`.

#### Scenario: image served with real content type
- **WHEN** a PNG attachment is downloaded
- **THEN** the response `Content-Type` SHALL be `image/png`

#### Scenario: unknown file served as octet-stream
- **WHEN** an attachment of a non-image/non-video type is downloaded
- **THEN** the response `Content-Type` SHALL be `application/octet-stream`

#### Scenario: SVG served with sandbox CSP
- **WHEN** an SVG attachment is downloaded
- **THEN** the response SHALL include a `Content-Security-Policy` that sandboxes the content

### Requirement: Downloads stream with HTTP Range support

Downloads SHALL stream via HTTP `Range` / `206 Partial Content` and SHALL NOT buffer
the entire file into memory.

#### Scenario: a range request is satisfied partially
- **WHEN** a client sends a `Range` header for a large download
- **THEN** the endpoint SHALL respond with `206 Partial Content` and the requested byte range

### Requirement: Missing files degrade to a fallback

The system SHALL return 404 for downloads whose underlying file is gone
(evicted for inbound, deleted by the agent for outbound), so the frontend
renders a fallback icon. This degradation SHALL be identical for inbound and
outbound.

#### Scenario: evicted inbound file returns 404
- **WHEN** an inbound file was evicted by the session budget and its download is requested
- **THEN** the endpoint SHALL return 404 (the record still exists in the transcript)

#### Scenario: deleted outbound file returns 404
- **WHEN** the agent deleted an outbound file and its download is requested
- **THEN** the endpoint SHALL return 404
