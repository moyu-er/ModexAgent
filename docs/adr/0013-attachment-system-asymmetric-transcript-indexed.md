# Attachment system: asymmetric inbound/outbound, transcript as the id→path index

Status: accepted

## Context

The bot's WebUI can only exchange text today. Files already move over IM (the QQ
adapter's native send, and a partial native receive), but the WebUI
**silently drops attachments** — `WebSocketOutputAdapter.send` handles only text
and approval envelopes; there is no upload path, no download serving, and no
durable, channel-independent way for the agent to reference a file the user sent
or the agent produced.

The **purpose** of this system is not file transfer in isolation. It is to let a
file that appears **inside a conversation** become something the **agent can
perceive and inspect**, and that the **user can download back** — symmetrically
across IM and WebUI. Upload/download is the infrastructure that serves that goal;
the goal itself is **conversation-level file awareness + tool-based inspection**.

We need, end to end:

- (a) WebUI upload and download with inline image/video preview and a **fallback
  icon** when a file is gone;
- (b) the **same file downloadable from both IM and WebUI** for one conversation;
- (c) the agent able to **receive, perceive, inspect, and persist** user files;
- (d) **size and quota** limits — a single **perception gate** (type + magic +
  size, shared by upload-accept, path-injection, and inline-render) plus a
  per-session byte budget;
- (e) a storage backend **swappable to object storage** (S3-class) later;
- (f) a **paved but dormant seam for native multimodal** — when a future provider
  supports vision/audio, matching attachments render into model content blocks
  without redesign; until then every attachment reaches the agent as a path
  reference it inspects with tools;
- (g) respect for the existing **workspace+pool** data partitioning and the
  framework/business layer split.

Existing pieces shape the design: `InputMessage.attachments` /
`OutputMessage.attachments` already carry `list[str]` paths; `SendFileToUserTool`
already pushes a path through `OutputAdapter.send` (works on IM, dropped on
WebUI); `AttachmentRef` + `UserInputEnvelope.attachments` already carry structured
inbound wire form; `ChatMessage.content` already supports `str | list[dict] | None`
(multimodal content blocks); and a pipeline-level `MediaProcessor` with pluggable
`MediaHandler` already renders images to vision blocks and extracts document text.
Its fate is decided below — it becomes the **dormant provider-side renderer**, the
seam requirement (f) asks for.

## Decision

### 1. The core: agent perception + tool-based inspection (v1 must-have)

The non-negotiable v1 capability chain:

1. A file arrives in a conversation (user upload, or agent-produced) and is
   persisted; an **Attachment** record is written to the transcript.
2. **`turn_context_builder.preprocess` injects a path reference into the user
   message** so the agent **perceives** the file — it knows the file exists, its
   name, MIME, size, and a **tool-usable absolute path**. This injection happens
   only for attachments that passed the perception gate (§7) and is **transient**:
   it enters the agent's LLM memory, not the persisted transcript user content
   (session management and agent memory differ by this one injection,
   intentionally).
3. The agent **chooses** which existing tool to invoke to inspect it (the bot's
   filesystem tools, an image-recognition MCP, a document tool, …).

**Boundary (load-bearing):** the attachment system is responsible only for
**making the agent aware of a file and handing it a path tools can read**. The
**viewing capability comes from the bot's existing tools**. The system provides no
viewer and forces no processing — it never decides *how* the agent handles a file,
only *that the agent knows about it and where it is*. The injected reference
carries `name + mime + size + absolute_path` (mime is the agent's cue for tool
selection); it deliberately does **not** carry the download `attachment_id`, which
is a frontend/download concern the agent never needs.

### 2. Two mechanisms for "the model understands a file" — they coexist

This is the distinction the design must keep explicit, because conflating them
breaks the seam.

- **Mechanism B — tool-based (v1, works with any model).** The model sees **only
  text**: a path reference, then a tool call carrying the path, then the tool's
  **text** result. The model **never sees pixels/bytes**. This is what the bot's
  existing image-recognition MCP does, and it is the v1 path for every
  attachment.
- **Mechanism A — native multimodal (deferred, needs a multimodal model).** The
  file's **bytes are inlined into the message as a structured content block** —
  e.g. `{"type":"image_url","image_url":{"url":"data:image/png;base64,..."},
  "_meta":{"path":...}}` alongside a text block. The model's built-in vision
  consumes the bytes directly; **no tool is involved**. This requires (i) a
  multimodal model, (ii) a **provider-side renderer** that turns an Attachment
  into such content blocks, and (iii) a capability switch plus strip-recovery.

The inputs are **not the same**: B is text throughout (path + tool text); A is a
multimodal content array (bytes inline). The two are **alternatives, not
replacements** — the tool path (B) stays useful even after A is activated (specialty
recognition, oversized images). v1 ships B only; A is the dormant seam of §10.

### 3. One concept, two asymmetric lifecycles

An **Attachment** is a file bound to a message, identified by an opaque id.
Direction-agnostic for rendering, but storage differs by direction:

- **Inbound** (user upload): bytes are persisted by a **MediaStore** under
  `<ws>/<data_dir>/media/<pool>/uploads/<session_id>/`, with a per-session bytes
  budget and a single-file cap (the storage gate, §7). The Attachment's
  `locator = media`.
- **Outbound** (agent-produced): the file **stays where the agent wrote it** (in
  place) and is **not copied**. Best-effort — if the agent deletes/overwrites it,
  the download degrades to a fallback icon. The Attachment's `locator = workspace`.

The asymmetry is deliberate: uploads have no natural workspace home and must be
budget-managed; agent output already lives on the filesystem the agent reads, and
copying it would duplicate without benefit.

**The download + degradation contract is fully symmetric** (this is the unifying
rule): one endpoint, one rule — *underlying file present → serve it; underlying
file gone → fallback icon* — regardless of direction or of *who* removed the file
(our budget eviction for inbound, the agent's deletion for outbound). The
asymmetry lives **only at ingestion/storage**; the `locator` field is an internal
read-dispatch detail (media → `MediaStore.read`, workspace → filesystem read) and
is invisible to the frontend.

### 4. Path semantics are per-locator (not universally workspace-relative)

- `locator = media`: `path` is **relative to the workspace root**, because the
  storage location is one we control (under `<data_dir>/media/...`). This is the
  only place the "relative to workspace root" rule applies.
- `locator = workspace`: `path` is the **literal absolute path** the agent
  provided (resolved by `SendFileToUserTool` against the bound workspace root at
  send time). **Any path is accepted** — no confinement, no copy. The workspace
  storage budget does **not** apply to outbound.

So the storage-path budget machinery is an **inbound-only** concern; outbound
records whatever path the agent gives and the download reads it verbatim.

### 5. Download: opaque-id capability URL + workspace as routing query param

Download is `GET /api/sessions/{session_id}/attachments/{attachment_id}?ws=<ws>`.
The `attachment_id` is an unguessable uuid and **is the capability** — no HMAC
signing, no auth round-trip — matching the WebUI's current unauthenticated model
(unguessability is the access control). `?ws=` is **routing only**, carrying zero
access-control weight: it is resolved by the existing single-source-of-truth
`_ws_root_of(ws_raw)` resolver, exactly like every other WebUI HTTP endpoint
(`/messages`, `/todos`, …). There is **no session→workspace reverse index**; the
frontend already holds `ws`, and a browser cannot attach a request body to
`<img src>` / `<a download>`, so a GET URL with a query param is unavoidable for
inline preview and click-to-download. `ws` is used to locate the transcript (to
find the id) and, for `locator=media`, the media directory.

**Serving safety — MIME allow-list.** Only `image/*` and `video/*` are served with
their real `Content-Type`; every other file is served as
`application/octet-stream` so a browser cannot sniff executable content out of an
unexpected type. SVG responses carry a strict `Content-Security-Policy`
(`default-src 'none'; sandbox; …`). This degrades nothing for the user —
non-image/video files download as opaque blobs, which is what `download` links do
anyway.

**Resumable / large downloads.** Downloads stream via HTTP `Range` / `206 Partial
Content` (the standard resumable-download mechanism; provided by the HTTP layer's
file response, not hand-rolled). This is required because outbound files may be up
to the outbound cap (§7). `MediaStore.read` is **stream/path-oriented**, never
loading a whole file into memory.

### 6. `MediaStore` — framework ABC, S3-swappable, routed like the other scoped stores

`MediaStore` lives in the framework with `save(stream, …) / read(id) → path|stream
/ delete / list / enforce_budget`, behind one ABC, with a `LocalFileMediaStore`
now and an object-storage backend later. `save`/`read` are **stream/path-oriented**
to support the largest configured file without buffering it in memory. A
per-(workspace,pool) resolver **mirrors** `WorkspaceScopedTranscriptStore.store_for`
and the session-store registry — a service-singleton with unified workspace+pool
routing, **not a parallel mechanism**. Only inbound bytes flow through it;
outbound reads the workspace filesystem directly. This is the seam requirement
(e) requires for a future object-storage backend.

### 7. One perception gate + a session budget — `MediaConfig` (frozen, per-pool override)

Inbound acceptance has two layers. Together they replace the earlier raw byte-cap
(a loose "100 MB" that admitted files the bot cannot perceive — useless dead
weight).

**Layer 1 — per-file perception gate (the "is this file useful to the bot"
filter).** This is the *one* gate that governs, with the same rules, three
consumers: upload **accept/reject**, **path-injection** (mechanism B), and
**inline render** (mechanism A). A file that cannot be perceived is **refused at
upload rather than stored**. Rules:

- **Type allow-list**: `image/*`, extractable-document, plain-text family are
  accepted; everything else is rejected. Magic-byte MIME is authoritative
  (§8) — a dangerous type revealed by magic bytes is rejected **regardless of
  extension** (disguise-evasion rejection).
- **Per-kind size cap** (defaults below, **configurable**): image ≤ **20 MB**;
  text/extractable-document ≤ **10 MB**. Over → hard reject at ingest (uploader
  is told). Accepted trade-off: a type-valid but oversized file (e.g. a 50 MB
  log) is rejected and the agent never gets to chunk-read it with tools; the user
  downsizes/splits first.

**Configurability — one config, both ends.** The size caps live in `MediaConfig`
(the single source of truth). The **backend enforces authoritatively** (size +
type + magic-byte + disguise rejection). The **backend exposes the active limits
to the frontend** (a config endpoint), and the **frontend pre-validates with the
fetched values** — looser by necessity, since a browser cannot magic-byte-sniff,
so it checks size + extension only for fast UX rejection. Both ends read the same
`MediaConfig`-derived numbers, so a limit change is one edit, not a divergent
frontend/backend hunt.

**Layer 2 — per-session budget (500 MB, configurable).** When a new upload would
push the session's inbound total over budget, **accept it, then delete oldest by
mtime** until total ≤ budget (uploader does not perceive). Budget key = the full
main-session id (`{conv}.main`) = the conversation (subagents do not receive user
uploads). Eviction is by **oldest mtime**, not LRU. This layer exists so endless
small uploads cannot fill the disk.

| Limit | Default | Behavior on exceed |
|---|---|---|
| Per-file perception gate (type+magic+size) | image ≤ 20 MB; text/doc ≤ 10 MB | **hard reject** at ingest (both ends) |
| Per-session inbound total | **500 MB** (configurable) | accept, then **delete oldest by mtime** to budget |
| Single outbound file | **1 GB** (configurable) | hard reject at `SendFileToUserTool` |
| Per-session count | — | **no count limit**; bytes only |

**Outbound** is in-place and **does not pass the perception gate** — the agent
produced it deliberately, so it is always recorded and downloadable; only the 1 GB
cap applies. There is **no separate download cap** (bounded by the upload/outbound
caps).

**Eviction consequence, accepted:** an evicted inbound file's Attachment record
remains in the transcript, so its download degrades to a fallback icon and the
agent's path reference may point at a now-missing file. This is the symmetric
best-effort rule of §3; it is a backstop, not the common path.

### 8. Magic-byte MIME is authoritative; three-way classification

`Attachment.mime` is determined by **magic-byte sniffing** (read the first bytes),
with extension as fallback only — extensions are not trusted. `Attachment.kind` is
a **three-way classification** computed from the authoritative MIME:

- **image** — magic-confirmed image (`image/png|jpeg|gif|webp|…`);
- **extractable-document** — text-extractable (pdf/docx/xlsx/pptx + plain-text
  family);
- **other** — everything else.

`kind` is the shared substrate for **both** the v1 path reference and the deferred
renderer of §10 — it is computed once at ingest and stored.

**Disguise rejection (storage-gate hardening).** If magic-byte sniffing reveals a
dangerous type regardless of the declared extension (e.g. a file named `.png`
whose bytes are a PE executable), the upload is **rejected at ingest** — this is
exactly the evasion the executable deny-list defends against. A mere
magic/extension disagreement that is *not* dangerous is logged as a warning
(possible misnaming) and proceeds using the magic MIME.

### 9. `ModelCapabilities` — capability enum, placeholder now, used later

The native-multimodal mechanism (§2-A) depends on a **per-provider/model
capability attribute the framework does not yet have**. We add it now as a
**placeholder, carried but unused**, so the deferred renderer has a concrete
switch to read later:

- A frozen `ModelCapabilities` value object on `LLMConfig`, exposing a set of
  `Modality` values.
- `Modality` is an enum: `TEXT` (always present), `IMAGE` / `VIDEO` / `AUDIO`
  (default-off). Extensible — adding a modality is one enum member.
- v1: every provider declares `TEXT` only; all other modalities are off. Nothing
  reads this field yet except a documented TODO seam. It exists so the model-facing
  gate (§10) and the renderer (§10) have a real switch to bind to in the future,
  rather than a boolean bolted on later.

### 10. Mechanism A seam — perception gate's inline-render consumer + dormant renderer

There is **one perception gate** (§7). It decides whether a file is accepted and
perceived at all. For an **accepted** attachment, *how* it reaches the model depends
on capability — and **only on capability**, since the gate already vetted type+size:

- **Mechanism B (v1, any model — the current implementation).** The attachment's
  path is **injected into the user message as a text reference**
  `[Attachment: name (mime, size) @ absolute_path]`. This is a **preprocess-time
  transient transform**: the agent's LLM history sees it, but the transcript stores
  the **original** user content plus the Attachment record — so **session
  management and agent memory differ by exactly this one injection, intentionally**
  (it affects memory, not the persisted conversation content). The agent then calls
  its tools (filesystem, image-recognition MCP, a document tool, …) to inspect the
  file. **This is the path for every attachment while all `Modality` flags are off,
  which is the entire v1.** No document text is auto-extracted into the message in
  v1 — the agent's tools do any parsing.
- **Mechanism A (deferred — design only, not implemented; see §10a).** When the
  `Modality` for an attachment's `kind` is on, the provider-side renderer inlines it
  as a model content block (image → `image_url`, document → extracted text), using
  the **same perception-gate type/size rules** (the file already passed them at
  accept). On model rejection, strip to a text placeholder.

**Document extraction** (lazy parser import, 200 K-char truncation, 50 MB extract
cap) belongs to the deferred renderer / the bot's tools, **not** to v1
path-injection.

**The dormant renderer contract (mechanism A).** When a modality is on and an
attachment of that `kind` passes the gate, the **provider-side renderer** (the
existing `MediaProcessor`, repurposed) turns the Attachment into model content:
images → `image_url` data-URL (or provider-URL) blocks, documents → extracted-text
blocks. **Every rendered block carries its source absolute path** (`_meta.path`).
On **model rejection** the renderer **strips the inline block back to a text
placeholder** `[image: <path>]` / `[doc: <path>]` using that path — a seamless
degradation to the mechanism-B form.

**Memory / history discipline (load-bearing — mechanism A).** A multimodal block
is **transient at call time only**; it is **never persisted as user/assistant
content** and is **never fed to memory consolidation or compression**. Three rules:

1. **Strip before persist.** Before a message is written to the agent LLM history,
   the renderer sanitizes any inline block back to its text placeholder. The LLM
   history therefore **never contains bytes or image URLs** — only short text
   placeholders. This generalizes the existing
   `inject_attachments_to_history` / `restore_multimodal_in_history` helpers
   (store placeholder, restore before send).
2. **Restore current turn only.** Only the **current user turn's** attachment is
   re-rendered into a real multimodal block at LLM-call time. Historical
   attachments **stay as text placeholders** — they are not re-inlined every turn
   (re-sending all historical images would be token-prohibitive). The model sees
   the live block for the file under discussion and cheap text markers for past
   files.
3. **The transcript is the durable index, not the LLM history.** The Attachment
   record lives in the append-only ServerEvent transcript (§11). Memory
   compaction/summarization operates on the text LLM history (placeholders); it
   never sees bytes, and if a placeholder is compacted away the attachment is
   **not lost** — its record remains in the transcript. Memory consolidation must
   **not treat an attachment as content to summarize**; at most it carries a short
   reference, because the bytes and the authoritative record both live elsewhere.

This seam is **paved, not built**: `ModelCapabilities` is the switch, the renderer
is the consumer, both are dormant in v1. Flipping a modality on later activates
inline rendering for that `kind` without touching the attachment domain model, the
download path, the transcript index, or the tool-based path.

### 10a. Phasing — mechanism A is the final, separately-spec'd item

Mechanism A (native multimodal: the `ModelCapabilities` switch, the perception
gate's inline-render consumer, the provider-side renderer, and the strip/restore
memory discipline above) is **deliberately the last implementation item and is not
implemented in the current track**. Two concrete gaps block it today:

- **Provider gap.** The framework's provider layer does not yet pass multimodal
  content blocks through to the underlying LLM API, and no `Modality` beyond
  `TEXT` is populated on any provider config.
- **No activation signal.** Until a real multimodal model is configured, the gate
  has nothing to bind to.

Everything ADR-0013 specifies for mechanism A — the `Modality` enum placeholder
(§9), the gate rules, the renderer contract, the memory discipline — is **preserved
as a stable contract** so a future, **separate spec** can implement against it
without redesigning the attachment domain, the download path, the transcript index,
or the v1 path-reference layer. The v1 deliverable is mechanism B (path reference +
agent tools) plus all the dormant seams and the `Modality` placeholder carried
unused. **Do not implement mechanism A in the current plan set.**

> Mechanism A has since been specified in **ADR-0014** (capability registry,
> current-turn-only inline, placeholder-on-disk). The contract above is what 0014
> implements against; this §10a's "deferred" is now "specified separately."

### 11. The transcript is the id→path index — and compression cannot touch it

Attachment records (`id`, `kind`, `name`, `mime`, `size`, `path`, `locator`) live
in the **append-only WebUI ServerEvent transcript** — inbound in the user-message
event, outbound in the assistant-turn event. Download resolves an id by scanning
that session's transcript events; **no separate attachment database**. Linear scan
for now; a per-session manifest is a later optimization only if a conversation's
attachment count makes scan cost noticeable.

Critically, the Attachment index lives in the **ServerEvent transcript, not the
agent's LLM message history**. Session compression (prune + archive) operates on
the agent LLM history; the ServerEvent transcript is append-only and is never
partially pruned, so **compression cannot orphan an Attachment record**. The agent
LLM history carries only the ephemeral text path-reference, which may be
compressed/archived freely — it is not the download index.

### 12. Inbound ingest is one shared pipeline stage; adapters are thin producers

There is **one** attachment-ingest stage in the input pipeline. Every channel
adapter (WebUI upload endpoint, IM native receive) is a **thin producer** that
hands the stage `AttachmentRef`s (+ bytes or source); the stage persists via
`MediaStore`, applies the storage gate (§7) + magic-byte MIME + classification
(§8), and writes the Attachment record. Adding a new channel never reimplements
download-or-budget logic — it only produces `AttachmentRef`s. The QQ adapter's
existing "download to an arbitrary local path" inbound code is **replaced** by
this shared stage, so IM-received files also get Attachment records and are
downloadable from WebUI (requirement b). A channel that cannot receive files at
all simply produces no attachments.

### 13. Transfer — reuse mature implementations, do not hand-roll

- **Download**: HTTP `Range` / `206 Partial Content` streaming (the standard
  resumable mechanism, supplied by the HTTP layer), plus the MIME allow-list of §5.
- **Upload (≤ 100 MB inbound)**: standard `multipart/form-data`. A resumable-upload
  protocol is **not** introduced in v1 — 100 MB does not justify the protocol
  state machine and staging area; if weak-link upload pain appears later, the
  `MediaStore.save(stream)` seam absorbs it without domain-model change.

Specific library choices are made at implementation time; this document names none.

## Framework / business split

- **Framework** (`modex_agent/`): `WorkspacePaths.media_dir(pool)` path primitive;
  `MediaStore` ABC + `LocalFileMediaStore` + the per-(ws,pool) resolver registry;
  `ModelCapabilities` / `Modality` on `LLMConfig`; the (deferred) provider-side
  renderer and the repurposed `MediaProcessor`; the path-reference injection
  helper; magic-byte MIME + `kind` classification; the shared ingest stage.
- **Business** (`bot/`): WebUI HTTP endpoints (upload, download, attachment-card
  delta envelope, and a **config endpoint exposing the active `MediaConfig` limits
  to the frontend**) and their wiring into the WS server; the MIME allow-list +
  `Range` serving policy on the download endpoint; **frontend pre-validation using
  the fetched shared limits** (size + extension, looser than the backend);
  `SendFileToUserTool` rendering; `MediaConfig` defaults and per-pool overrides;
  the QQ adapter's `AttachmentRef` production.

## Consequences

- Inbound and outbound look symmetric to the frontend (one Attachment, one
  download endpoint, one fallback path) but are managed differently on disk; the
  single symmetric rule is *file present → serve, file gone → fallback icon*, and
  `locator` is an internal read-dispatch switch.
- Outbound downloads are best-effort: a file the agent overwrites or deletes
  becomes a fallback icon. Inbound downloads are best-effort for the same reason
  once budget eviction removes the file. Both are the same degradation.
- Outbound accepts any literal absolute path, unconfinable and uncopied; the
  workspace storage budget is inbound-only.
- **Until a `Modality` beyond `TEXT` is enabled, images and all other attachments
  reach the agent only as path references** — the agent must use tools to look at
  any attachment. This is a deliberate v1 simplification: the path-reference layer
  (mechanism B) works with any model and is the foundation; native multimodal
  (mechanism A) activates later by flipping a capability and binding the dormant
  renderer, with no change to the attachment domain model, download path, or
  tool-based path.
- **One perception gate** (type allow-list + magic-byte MIME + per-kind size,
  configurable, shared by frontend and backend via one `MediaConfig`) governs
  upload accept/reject, path-injection (mechanism B), and inline-render (mechanism
  A). A per-session byte budget (500 MB, oldest-by-mtime eviction) is the disk
  backstop. Outbound bypasses the perception gate (agent-produced). Sizes are
  configurable in one place; the backend exposes them to the frontend so both ends
  agree.
- A multimodal block is **transient**: it is stripped to a text placeholder before
  the message is persisted and is never re-rendered for past turns. Memory and
  compression therefore never see bytes; the durable attachment is the transcript
  record, so compaction cannot lose it.
- The transcript gains Attachment records in both user and assistant events; this
  is the append-only ServerEvent transcript, immune to session compression.
  Serialization treats records as metadata (paths/refs, never bytes); base64 never
  enters stored history — only the ephemeral, restorable inline form does, and only
  once mechanism A is active.
- `MediaStore.save/read` are stream/path-oriented; the largest configured file
  (1 GB outbound) never buffers whole into memory.
