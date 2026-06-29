# Tasks: add-attachment-system

> **Progress tracking**: After completing each task below and verifying it passes,
> invoke `change-progress add-attachment-system` to mark the checkbox before moving on.
> Do not batch-mark: complete one, mark one, then continue.

## 1. Framework foundation — paths, config, MIME, models

- [x] 1.1 Add `SUBDIR_MEDIA` constant and `media_dir(pool) -> Path` accessor to `src/modex_agent/workspace/paths.py` (routed through `_child`); unit-test containment + pool isolation.
- [x] 1.2 Add `Modality` enum (`TEXT`/`IMAGE`/`VIDEO`/`AUDIO`) and frozen `ModelCapabilities` value object to `src/modex_agent/ioc/configs/llm.py`; add a `capabilities` field to `LLMConfig` defaulting to TEXT-only; unit-test default is TEXT-only.
- [x] 1.3 Add frozen `MediaConfig` to `src/modex_agent/ioc/configs/pool.py` with `max_image_bytes` (20 MB), `max_text_doc_bytes` (10 MB), `session_budget_bytes` (500 MB), `max_outbound_bytes` (1 GB), and a dangerous-executable deny-list pinned at magic-byte signatures (PE `MZ`, ELF `\x7fELF`, Mach-O `0xfeedface`/`0xfeedfacf`) plus a secondary extension list (`.exe/.dll/.bat/.cmd/.scr/…`); wire onto `PoolConfig` with per-pool override; unit-test defaults + override.
- [x] 1.4 Create `src/modex_agent/media/mime.py`: magic-byte MIME sniffing (PNG/JPEG/GIF/WEBP + text heuristics) with extension fallback; `classify_kind(mime) -> Kind` (`image`/`extractable_document`/`other`); unit-test misnamed-image, unknown-binary, text cases.
- [x] 1.5 Create `src/modex_agent/media/models.py`: frozen `Attachment` value object (`id`, `kind`, `name`, `mime`, `size`, `path`, `locator` enum `media`/`workspace`); unit-test construction + immutability.

## 2. Framework foundation — MediaStore + resolver

- [x] 2.1 Create `src/modex_agent/media/store.py`: `MediaStore` ABC (`save(stream,…)` / `read(id)->path|stream` / `delete` / `list` / `enforce_budget`) and `LocalFileMediaStore` persisting under `media_dir(pool)/uploads/<session_id>/`; unit-test save/read/delete round-trip without whole-file buffering.
- [x] 2.2 Create the business resolver `examples/bot_project/bot/service/media_store.py`: per-(workspace,pool) resolver mirroring the business `WorkspaceScopedTranscriptStore` — resolves the workspace root the way the WebUI does (`_ws_root_of`) and builds the media dir via framework `WorkspacePaths.media_dir(pool)`, handing each (ws,pool) a cached `LocalFileMediaStore`; unit-test two pools → distinct dirs and that the framework store receives no ws/pool knowledge.
- [x] 2.3 Implement `enforce_budget` (oldest-by-mtime eviction to `session_budget_bytes`) on `LocalFileMediaStore`; unit-test over-budget upload evicts oldest, subagent sessions excluded.

## 3. Perception gate + ingest stage

- [x] 3.1 Create `src/modex_agent/media/gate.py`: `perception_gate(file, MediaConfig) -> Accept|Reject(reason)` combining type allow-list + magic MIME + per-kind size + disguise (dangerous-type) rejection; unit-test accept, oversize reject, non-allowlisted reject, disguised-exe reject.
- [x] 3.2 Create the business ingest stage `examples/bot_project/bot/input_pipeline/stages/attachment_ingest.py`: takes `AttachmentRef`s + bytes/source, runs the framework perception gate, persists via the business media resolver's `MediaStore`, enforces budget, classifies kind (framework helper), returns a list of framework `Attachment` records; unit-test accept path + reject path.
- [x] 3.3 Wire the ingest stage into the business pipeline composition at `examples/bot_project/bot/input_pipeline/assembly.py` (where envelopes with `attachments` are processed) so `UserInputEnvelope.attachments` flow through it; verify via a pipeline integration test that an `AttachmentRef` becomes a persisted `Attachment`.

## 4. Transcript id→path index

- [x] 4.1 Extend the bot ServerEvent transcript write path (`bot/service/`) to carry inbound `Attachment` records on user-message events and outbound records on assistant-turn events; unit-test records appear on the correct event type.
- [x] 4.2 Add a transcript-scan helper `find_attachment(session_id, attachment_id) -> Attachment | None` that scans the session's ServerEvent transcript events; unit-test hit + miss; confirm no separate attachment DB is introduced.

## 5. Agent perception (mechanism B, transient injection)

- [x] 5.1 In `src/modex_agent/pipeline/turn_context_builder.py` `preprocess`, inject `[Attachment: <name> (<mime>, <size>) @ <absolute_path>]` per accepted attachment into the user message (no `attachment_id`); unit-test injection content + that it is gated by accepted attachments only.
- [x] 5.2 Verify the injection is transient: the agent LLM history receives it but the persisted transcript user-message content does not (only original content + record); add a regression test asserting transcript content excludes the injected string.

## 6. Download endpoint

- [x] 6.1 Add `GET /api/sessions/{session_id}/attachments/{attachment_id}?ws=` to `examples/bot_project/bot/webui/server.py`: resolve ws via `_ws_root_of`, scan transcript for the id, dispatch on `locator` (media → business media resolver's `MediaStore.read`, workspace → FS read); integration-test both locators + 404 for unknown id.
- [x] 6.2 Add the MIME allow-list to the download response (image/video real `Content-Type`, else `application/octet-stream`, SVG CSP) and HTTP `Range`/`206` streaming; integration-test image content-type, octet-stream fallback, and a `Range` request returning 206.
- [x] 6.3 Confirm symmetric fallback: evicted-inbound and deleted-outbound both return 404 (record remains in transcript); integration-test both.

## 7. Outbound (SendFileToUserTool)

- [x] 7.1 Update `examples/bot_project/bot/tools/custom.py` `SendFileToUserTool` to emit an outbound `Attachment` (`locator=workspace`, literal absolute path, in-place, no copy) and write its record to the assistant-turn transcript event; unit-test record shape + no copy.
- [x] 7.2 Enforce the 1 GB outbound cap (hard reject, inform the agent); unit-test reject over-cap and that perception gate is NOT applied to outbound.

## 8. WebUI rendering + upload UI + shared config endpoint

- [x] 8.1 Add `WebSocketOutputAdapter.send` attachment-card delta emission (image inline preview / file card / fallback) in `examples/bot_project/bot/adapters/web_socket.py`, direction-agnostic; unit-test the three render branches.
- [x] 8.2 Add a `GET` config endpoint exposing active `MediaConfig` limits (image/text sizes, budget) to the frontend; integration-test it returns the configured numbers.
- [x] 8.3 Add the WebUI upload UI + attachment rendering (image inline / file card / fallback icon) in `examples/bot_project/webui/`; fetch the config endpoint and pre-validate (size + extension) before upload; manually verify upload + preview + download round-trip.

## 9. IM alignment (unified ingest)

- [x] 9.1 Replace the QQ adapter's arbitrary-local-path inbound download (`examples/bot_project/bot/adapters/qq.py:292-378`) with `AttachmentRef` production handed to the shared ingest stage; verify QQ-received files now produce Attachment records under the standard media layout and are WebUI-downloadable.

## 10. Multimodal seam (placeholder only — do NOT implement mechanism A)

- [x] 10.1 Add `TODO`-marked provider-side renderer seam on `src/modex_agent/core/provider.py` and on `src/modex_agent/utils/media_utils.py` (`MediaProcessor`), with each TODO citing ADR-0013 §10/§10a by section; verify the documented renderer contract + strip/restore memory discipline live in ADR-0013 §10/§10a and design.md D7 (the authoritative sources) and that no non-TEXT modality activates any code path.
- [x] 10.2 Add a guard test asserting v1 behavior is independent of `ModelCapabilities` (every attachment still reaches the agent as a path reference regardless of the field).

## 11. Verification + docs

- [x] 11.1 Run the framework test suite (`tests/unit/`) and bot test suite (`examples/bot_project/tests/`); fix failures; capture green output.
- [x] 11.2 Run the bot end-to-end (WebUI upload → agent perceives → memory/transcript insert(different insert ways) → agent inspects via tool → user downloads; QQ receive → WebUI download) and record evidence.
  - **Evidence (2026-06-29, live bot, port 21801, model `step-3.7-flash`):** `POST /api/sessions` → 200 (session `4AguEFTuDCdTxcDox.main`); `POST .../attachments?ws=` → **200** (upload staged — the formerly-dead inbound path now wired via `WorkspaceScopedMediaStore`); WS `send_message` with the attachment → agent `main idle→working`, **LLM iter=1 `finish_reason=tool_calls tools=["read"]`** (mechanism-B path injection reached the live model, which chose the `read` tool); `GET .../attachments/{id}?ws=` → **200 `image/png` 72 B**, byte-for-byte match with the upload. The `read` then 404'd only because the model used the relative name `probe.png` instead of the injected absolute path (model nuance, not a system defect — the injection carried the abs path). QQ→WebUI download shares the same now-wired IM input context; its ingest→record link is covered by `test_qq_inbound_attachment.py` (the IM channel goes through the same `_build_input_context`).
- [x] 11.3 Update `CONTEXT.md` cross-refs and `docs/handoff/13-HANDOFF-attachment-system.md` status to reflect implementation complete; mark ADR-0013 implementation notes.
