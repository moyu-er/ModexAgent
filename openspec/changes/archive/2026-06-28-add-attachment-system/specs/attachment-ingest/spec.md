## ADDED Requirements

### Requirement: One perception gate governs upload acceptance

A single perception gate SHALL decide whether an inbound file is accepted. The gate
SHALL apply, with the same rules, to upload accept/reject, path-injection, and
(future) inline-render. The gate SHALL combine: a type allow-list (image,
extractable-document, plain-text family), magic-byte MIME detection, and a per-kind
size cap. A file that cannot be perceived SHALL be rejected at ingest rather than
stored.

#### Scenario: type-valid file within size cap is accepted
- **WHEN** a 5 MB PNG is uploaded
- **THEN** it SHALL pass the perception gate and be persisted

#### Scenario: oversized type-valid file is rejected
- **WHEN** a 50 MB plain-text file is uploaded (text cap is 10 MB)
- **THEN** ingest SHALL reject it with a size error and the uploader SHALL be informed

#### Scenario: non-allowlisted type is rejected
- **WHEN** a file whose magic-byte MIME is outside the allow-list is uploaded
- **THEN** ingest SHALL reject it

### Requirement: Magic-byte MIME is authoritative and classifies kind

The attachment MIME type SHALL be determined by magic-byte sniffing, with file
extension as fallback only. Each attachment SHALL be classified into exactly one
`kind`: `image`, `extractable-document`, or `other`, computed from the authoritative
MIME.

#### Scenario: misnamed image is detected by magic bytes
- **WHEN** a PNG whose extension is `.txt` is uploaded
- **THEN** its MIME SHALL be recorded as `image/png` and its kind as `image`

#### Scenario: unknown binary is classified as other
- **WHEN** a file with no recognizable magic bytes and an unknown extension is uploaded
- **THEN** its kind SHALL be `other`

### Requirement: Disguised dangerous executables are rejected regardless of extension

If magic-byte sniffing reveals a dangerous executable type, ingest SHALL reject the
file regardless of its declared extension (disguise-evasion rejection).

#### Scenario: executable disguised as image is rejected
- **WHEN** a PE executable renamed to `.png` is uploaded
- **THEN** ingest SHALL reject it even though the extension is allowlisted

### Requirement: MediaConfig is the single source shared by frontend and backend

Per-kind size caps and the session budget SHALL live in a frozen `MediaConfig`,
overridable per pool. The backend SHALL enforce authoritatively. The backend SHALL
expose the active limits to the frontend, and the frontend SHALL pre-validate using
the fetched values (looser — size + extension only, since a browser cannot
magic-byte-sniff). Both ends SHALL read the same `MediaConfig`-derived numbers.

#### Scenario: changing a size cap updates both ends from one config
- **WHEN** the image size cap is changed in `MediaConfig`
- **THEN** both the backend enforcement and the frontend pre-validation SHALL use the new value without separate edits

#### Scenario: frontend pre-rejects an oversized file before upload
- **WHEN** the user selects a file larger than the fetched image cap in the browser
- **THEN** the frontend SHALL block the upload with a size message

### Requirement: A per-session byte budget evicts oldest-by-mtime

The system SHALL enforce a per-session byte budget. When a new upload would push
the session's inbound total over the 500 MB (configurable) budget, ingest SHALL
accept the upload, then delete oldest-by-mtime
files until the total is within budget. The uploader SHALL NOT be notified of the
eviction. The budget key SHALL be the full main-session id.

#### Scenario: over-budget upload evicts the oldest file
- **WHEN** a session at 490 MB receives a 20 MB upload (budget 500 MB)
- **THEN** the new upload SHALL be accepted and the oldest previously-stored file SHALL be deleted to bring the total within budget

#### Scenario: subagent sessions do not share the main session budget
- **WHEN** a subagent session `{conv}.reviewer.x` exists alongside main `{conv}.main`
- **THEN** uploads SHALL be budgeted against `{conv}.main`, not the subagent session

### Requirement: A shared input-pipeline ingest stage unifies WebUI and IM

One attachment-ingest stage SHALL exist in the input pipeline. Every channel adapter
(WebUI upload endpoint, IM native receive) SHALL be a thin producer that hands the
stage `AttachmentRef`s plus bytes/source; the stage SHALL persist via `MediaStore`,
apply the perception gate, classify kind, and write the Attachment record. The QQ
adapter's existing arbitrary-local-path inbound download SHALL be replaced by this
stage.

#### Scenario: WebUI and IM produce the same record shape
- **WHEN** the same image arrives via WebUI upload and via QQ native receive
- **THEN** both SHALL flow through the ingest stage and produce Attachment records of the same shape, stored under the same media layout

#### Scenario: a channel that cannot receive files produces no attachments
- **WHEN** a channel does not implement file reception
- **THEN** it SHALL simply produce no `AttachmentRef`s, and ingest SHALL not be required of it
