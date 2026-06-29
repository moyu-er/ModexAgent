## Context

ADR-0013 (accepted) settles the attachment-system design. This change implements
it. The existing scaffolding: `InputMessage`/`OutputMessage.attachments: list[str]`,
`SendFileToUserTool` (works on IM, dropped on WebUI), a QQ adapter with native send
and a partial native receive (downloads to an arbitrary local path), an
`AttachmentRef` + `UserInputEnvelope.attachments` wire form, `ChatMessage.content`
already supporting `str | list[dict]` multimodal blocks, a `MediaProcessor` + pluggable
`MediaHandler`, and `inject_attachments_to_history` / `restore_multimodal_in_history`
placeholder-restore helpers. The WebUI ServerEvent transcript store is append-only
and never partially pruned; workspace+pool routing already exists in
`WorkspaceScopedTranscriptStore` and `_ws_root_of`.

Constraints: framework/business layer split (`src/modex_agent/` vs
`examples/bot_project/bot/`), frozen config/value objects, enums over strings, ABCs
over Protocols, surgical changes, no speculative generality. Documents must not name
external projects.

## Goals / Non-Goals

**Goals:**
- Conversation-level file awareness: an uploaded file is perceived by the agent
  (transient path reference) and inspectable with its tools; the user can download
  it back, symmetrically across IM and WebUI.
- One perception gate (type + magic-byte MIME + per-kind size, configurable, shared
  FE/BE) governing accept, path-injection, and (future) inline-render.
- Asymmetric storage (inbound MediaStore + budget; outbound in-place) with symmetric
  download + degradation.
- A durable, compression-immune id→path index in the append-only transcript.
- A dormant, documented native-multimodal seam (mechanism A) carried as placeholder.

**Non-Goals:**
- Implementing native multimodal (mechanism A) — placeholder only this change.
- Object-storage backend — ABC seam only; `LocalFileMediaStore` ships now.
- Document text extraction into the message in v1 — the agent's tools parse.
- Resumable upload protocol (tus) — standard multipart in v1.

## Decisions

### D1. One perception gate replaces the earlier two-gate + 100 MB model
**Decision:** A single gate (type allow-list + magic-byte MIME + per-kind size) is
the upload accept/reject decision AND governs path-injection (mechanism B) and
inline-render (mechanism A). A 500 MB per-session budget with oldest-mtime eviction
is the disk backstop.
**Why:** The earlier "100 MB single-file" cap admitted files the bot cannot perceive
— useless dead weight. Tying acceptance to perceptibility (type+size) refuses junk
upfront. One gate for three consumers avoids divergent limits.
**Alternatives:** (a) Keep a loose storage cap plus a separate model-facing gate —
rejected: allows un-perceivable files, two limits to keep in sync. (b) Drop the
session budget entirely — rejected: endless small uploads fill the disk.
**Trade-off accepted:** a type-valid but oversized file (e.g. 50 MB log) is rejected;
the agent never gets to chunk-read it with tools. The user downsizes/splits first.

### D2. Download URL is capability-id + `?ws=` routing (no reverse index)
**Decision:** `GET /api/sessions/{sid}/attachments/{aid}?ws=<ws>`. The uuid id is the
capability; `?ws=` is routing only, resolved by the existing `_ws_root_of`.
**Why:** Every existing WebUI HTTP endpoint already resolves workspace from `?ws=`;
there is no session→workspace reverse index and building one would be a parallel
mechanism to maintain. Browsers cannot attach a body to `<img src>`/`<a download>`,
so a GET URL is unavoidable for inline preview.
**Alternatives:** (a) id-only URL with a real session→workspace index — rejected:
no such index exists; it would duplicate routing state. (b) HMAC-signed URL —
rejected: webui is unauthenticated, so an unguessable id is equivalent capability
without signing overhead.

### D3. Path semantics are per-locator, not universally workspace-relative
**Decision:** `locator=media` paths are relative to the workspace root (a location we
control); `locator=workspace` paths are the literal absolute path the agent gave.
**Why:** Outbound is in-place and unconfinable; forcing relative-to-root would
forbid legitimate absolute paths. Inbound is confined because we own its location.
**Alternatives:** Force all paths relative-to-root — rejected: breaks "send any file"
outbound and `WorkspacePaths._child` would reject escapes.

### D4. The transcript is the id→path index; the agent LLM history carries only text
**Decision:** Attachment records live in the append-only ServerEvent transcript
(user-message / assistant-turn events). The agent LLM history carries only ephemeral
text references. The injected path reference is a preprocess-time transient
transform — agent-memory-only, not persisted transcript content.
**Why:** Compression prunes the agent LLM history but never the append-only
transcript, so records survive. Keeping the injection out of the transcript content
preserves the original user message; the record is the durable bit.
**Alternatives:** A separate attachment database — rejected: violates locality and
adds a second source of truth to keep consistent with the transcript.

### D5. Shared ingest stage; adapters are thin AttachmentRef producers
**Decision:** One ingest stage in the input pipeline; WebUI upload and IM receive
both produce `AttachmentRef`s + bytes and hand them to it. The QQ arbitrary-local-path
download is replaced.
**Why:** Achieves IM/WebUI symmetry without per-channel reimplementation; a new
channel only produces `AttachmentRef`s.
**Alternatives:** Per-channel ingest — rejected: divergent behavior, duplicate
gate/budget logic.

### D6. Stream/path-oriented MediaStore; MIME allow-list + Range serving
**Decision:** `MediaStore.save/read` are stream/path-oriented (no whole-file buffer).
Downloads stream via HTTP Range/206; serving applies a MIME allow-list (image/video
real MIME, else `application/octet-stream`, SVG CSP).
**Why:** Outbound files may be up to 1 GB — must not buffer in memory. The MIME
allow-list prevents browser content-sniffing of unexpected types.
**Alternatives:** bytes-oriented MediaStore — rejected: cannot support 1 GB.

### D7. ModelCapabilities/Modality is a dormant placeholder; mechanism A is design-only
**Decision:** Add `ModelCapabilities`/`Modality` on `LLMConfig` (TEXT always, others
default-off), carried unused. Document the renderer contract + strip/restore memory
discipline. Do NOT implement mechanism A.
**Why:** The provider layer cannot yet pass multimodal content blocks and no
multimodal model is configured — nothing to bind to. Carrying the placeholder now
gives the future spec a stable switch; building it now is speculative.
**Alternatives:** Defer even the placeholder — rejected: a later boolean bolted on
is messier than a designed enum carried now.

### D8. MediaStore ABC in framework; the per-(ws,pool) resolver in business
**Decision:** The framework ships the `MediaStore` ABC + `LocalFileMediaStore`
(operating on a resolved directory) and the `WorkspacePaths.media_dir(pool)` path
primitive. The **per-(workspace,pool) resolver** lives in the **business** layer
(`bot/service/`), mirroring the business `WorkspaceScopedTranscriptStore`, and
resolves the workspace root the same way the WebUI does (the `_ws_root_of` path
already used by every WebUI HTTP endpoint).
**Why:** `WorkspaceScopedTranscriptStore` and `_ws_root_of` already live in the
business layer — the framework has no ws-keyed root resolver and should not gain
one (workspace routing is a cross-cutting business concern, per
`.claude/rules/architecture.md` §9 and the existing transcript/session-store
precedent). Keeping the resolver in business next to the transcript store preserves
locality and avoids a parallel framework mechanism. The framework's job is the
reusable, backend-swappable byte store + path primitive; the business's job is
turning an incoming request's `ws` into the right directory.
**Alternatives:** (a) Put the resolver in the framework and have it call the
business `_ws_root_of` — rejected: inverts the dependency and drags a business
symbol into framework code. (b) Add a framework ws-root resolver ABC — rejected:
speculative seam with one caller; violates "one adapter is a hypothetical seam."

## Risks / Trade-offs

- **[Eviction orphans inbound references]** → accepted: evicted files degrade to
  fallback; 500 MB is generous for a single user; the record remains in transcript.
- **[Outbound file deleted by agent]** → accepted: best-effort fallback, same as
  inbound eviction (symmetric degradation).
- **[Magic-byte sniffing cost / false negatives]** → sniff first bytes only; fall
  back to extension; disguise-rejection catches the security-relevant case.
- **[Frontend/backend limit drift]** → single `MediaConfig` source; backend exposes
  limits to frontend; backend is authoritative.
- **[Transcript scan cost for download]** → linear scan now; per-session manifest is
  a later optimization only if a conversation's attachment count makes it noticeable.
- **[Mechanism-A seam bit-rots while dormant]** → keep `MediaProcessor` + helpers as
  a real adapter seam (base + subclasses), not deleted; TODO marks the bind point.

## Migration Plan

- Net-new modules and fields with defaults; no breaking config changes. Existing
  `InputMessage`/`OutputMessage.attachments: list[str]` remains; Attachment records
  are additive metadata in the transcript.
- The QQ adapter's local-download path is replaced by the shared ingest stage in the
  same change (no compat shim) — the bot is single-user, clean-over-compat.
- Rollback: revert the change; old transcripts (without Attachment records) remain
  readable; download endpoints simply 404.

## Open Questions

- The dangerous-executable deny-list is pinned at the **magic-byte signature**
  level (PE `MZ`, ELF `\x7fELF`, Mach-O `0xfeedface`/`0xfeedfacf`), with magic-byte
  detection authoritative over extension per the ingest spec. The extension deny-list
  (`.exe/.dll/.bat/.cmd/.scr/…`) is a secondary, best-effort layer; its exact members
  are finalized at task 1.3 with a security review.
- Whether the session budget eviction should also warn when a still-referenced file
  is evicted (currently silent by design) — defer until observed in practice.
