## Why

The bot's WebUI can only exchange text. Files move over IM (QQ native send, plus a
partial native receive), but the WebUI **silently drops attachments**
(`WebSocketOutputAdapter.send` handles only text and approval envelopes), and there
is no durable, channel-independent way for the agent to reference a file the user
sent or the agent produced. The goal is not file transfer in isolation — it is
**conversation-level file awareness**: a file that appears inside a conversation
becomes something the **agent can perceive and inspect with its tools**, and that
the **user can download back**, symmetrically across IM and WebUI. Design is settled
in **ADR-0013** (accepted); this change implements it.

## What Changes

- Add a framework **MediaStore** ABC + `LocalFileMediaStore`, per-(workspace,pool)
  routed (mirroring `WorkspaceScopedTranscriptStore`), stream/path-oriented,
  swappable to object storage later; add `WorkspacePaths.media_dir(pool)`.
- Add a **single perception gate** (type allow-list + magic-byte MIME + per-kind
  size, configurable via `MediaConfig`, shared by frontend and backend) governing
  upload accept/reject, path-injection, and (future) inline-render; plus a 500 MB
  per-session inbound budget with oldest-by-mtime eviction.
- Add **magic-byte MIME detection** (authoritative over extension) + **three-way
  `kind` classification** (image / extractable-document / other); reject disguised
  dangerous executables regardless of extension.
- Add a **shared input-pipeline ingest stage** so WebUI upload and IM native receive
  both produce `AttachmentRef`s and flow through one stage; **replace** the QQ
  adapter's existing arbitrary-local-path download with this stage.
- Add the **Attachment record** as the id→path index in the **append-only WebUI
  ServerEvent transcript** (inbound in user-message events, outbound in
  assistant-turn events) — no separate attachment database, immune to session
  compression.
- Add the **download endpoint** `GET /api/sessions/{session_id}/attachments/{attachment_id}?ws=<ws>`
  (uuid id = capability, `?ws=` = routing via `_ws_root_of`), dispatching on
  `locator` (media → `MediaStore.read`, workspace → filesystem), with a **MIME
  allow-list** (image/video real MIME, else `application/octet-stream`, SVG CSP)
  and **HTTP Range/206** streaming.
- Add **asymmetric storage**: inbound `locator=media` (relative-to-root, budgeted);
  outbound `locator=workspace` (literal absolute path, in-place, uncopied, 1 GB
  cap). Download + degradation are symmetric (file present → serve, gone → fallback
  icon).
- Add **agent perception (mechanism B, v1)**: `turn_context_builder.preprocess`
  injects a transient path reference `[Attachment: name (mime, size) @ absolute_path]`
  into the user message — agent-memory-only, not persisted transcript content; the
  agent chooses tools to inspect.
- Wire **`SendFileToUserTool`** to emit an outbound Attachment (workspace locator,
  any absolute path, in-place, 1 GB cap).
- Render the WebUI **attachment-card delta** (image inline / file card / fallback
  icon) for any Attachment, symmetric across inbound and outbound; add the frontend
  upload UI and attachment display.
- **Mechanism A (native multimodal) is design-only in this change — not implemented;
  deferred to a separate spec.** This change adds only a
  **`ModelCapabilities`/`Modality` placeholder** on `LLMConfig` (TEXT always,
  IMAGE/VIDEO/AUDIO default-off, carried unused) as the dormant switch, plus the
  **documented** (not built) provider-side renderer contract + strip/restore memory
  discipline. The provider layer cannot yet pass multimodal content blocks.

## Capabilities

### New Capabilities

- `attachment-storage`: Framework `MediaStore` ABC + `LocalFileMediaStore` +
  per-(workspace,pool) resolver + `WorkspacePaths.media_dir`; inbound byte
  persistence, stream/path-oriented, S3-swap seam. Outbound reads the filesystem
  in place.
- `attachment-ingest`: The shared input-pipeline ingest stage — perception gate
  (type + magic-byte MIME + per-kind size), `MediaConfig` (configurable, one source
  for frontend + backend), session budget + oldest-by-mtime eviction, `kind`
  classification, disguise rejection; unified WebUI + IM `AttachmentRef` production.
- `attachment-transcript`: Attachment records as the id→path index in the
  append-only ServerEvent transcript (user-message / assistant-turn events),
  compression-immune; the agent LLM history carries only ephemeral text references.
- `attachment-download`: The download endpoint (capability id + `?ws=` routing),
  `locator` dispatch, MIME allow-list + octet-stream degradation + SVG CSP, Range/206
  streaming; symmetric best-effort fallback.
- `attachment-agent-perception`: Mechanism B path-reference injection at preprocess
  time — transient (agent memory, not transcript content), agent selects tools to
  inspect the file.
- `attachment-outbound`: `SendFileToUserTool` emits a workspace-locator Attachment
  (any absolute path, in-place, 1 GB cap) — outbound *production* only.
- `attachment-rendering`: WebUI attachment-card delta rendering for any Attachment
  (image inline / file card / fallback icon), symmetric across inbound and
  outbound; frontend upload UI and attachment display.
- `model-multimodal-seam`: `ModelCapabilities`/`Modality` placeholder enum on
  `LLMConfig` + the dormant provider-side renderer contract + strip/restore memory
  discipline for native multimodal (mechanism A). **Placeholder only — not
  implemented in this change; design preserved for a future spec.**

### Modified Capabilities

(None — no existing specs.)

## Impact

- **Framework** (`src/modex_agent/`): new `media` module — `MediaStore` ABC +
  `LocalFileMediaStore` (operating on a resolved directory, no ws/pool knowledge),
  magic-byte MIME + `kind` classification, perception gate, `Attachment` model;
  `WorkspacePaths.media_dir`; `ModelCapabilities`/`Modality` on `ioc/configs/llm.py`;
  `MediaConfig` on `ioc/configs/pool.py`; path-reference injection in
  `pipeline/turn_context_builder.preprocess`; TODO seams on the provider and
  `MediaProcessor`.
- **Business** (`examples/bot_project/bot/`): the per-(workspace,pool) media
  **resolver** in `service/` (mirroring `WorkspaceScopedTranscriptStore`); the
  ingest **stage** in `input_pipeline/stages/` + its wiring in
  `input_pipeline/assembly.py`; WebUI upload/download/config endpoints + MIME
  allow-list + Range serving in `webui/server.py`; attachment-card delta in
  `adapters/web_socket.py`; `AttachmentRef` production + ingest-stage replacement in
  `adapters/qq.py`; `SendFileToUserTool` rendering in `tools/custom.py`; transcript
  Attachment-record write in `service/`.
- **Frontend** (`examples/bot_project/webui/`): upload UI, attachment rendering
  (image inline / file card / fallback), fetching shared limits for pre-validation.
- **Tests**: framework unit tests for MediaStore, perception gate, classification,
  injection; bot integration tests for upload/download/ingest/Card rendering.
- **Dependencies**: none new at v1 (multipart + Range via the existing HTTP layer);
  document extraction and object-storage backends are explicitly out of scope.
