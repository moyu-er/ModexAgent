"""Attachment ingest stage — gate, persist, and classify inbound attachments.

Consumes ``envelope.attachments`` (channel-produced :class:`AttachmentRef`s,
each pointing at a temp file holding the raw bytes via ``local_path``), runs
each through the framework perception gate (ADR-0013 §7), and for the accepted
ones:

- persists the bytes through the workspace+pool-routed
  :class:`WorkspaceScopedMediaStore` (framework ``MediaStore.save``),
- enforces the per-session inbound budget (oldest-by-mtime eviction),
- builds a framework :class:`Attachment` record (``locator=MEDIA``,
  ``path`` relative to the workspace root, ADR §4), and
- appends it to ``envelope.resolved_attachments`` — the typed handoff to the
  transcript-write stage (G4) and the agent-perception injection (G5).

Rejected attachments are skipped (logged) — never stored, never recorded. The
stage is a pure pass-through when there are no attachments, so legacy
envelopes and decision/control envelopes flow unaffected.

Runs after :class:`ResolvePoolStage` (needs ``RESOLVED_POOL`` /
``FULL_SESSION_ID`` / ``WORKSPACE`` on the envelope) and before
:class:`PersistUserMessageStage`. One stage serves both IM and WebUI — channel
adapters only differ in how they populate ``AttachmentRef``.
"""

from __future__ import annotations

import logging
import uuid
from pathlib import Path

from bot.input_pipeline.context import BotInputContext
from bot.input_pipeline.stages.resolve_pool import RoutingMeta
from bot.service.media_store import WorkspaceScopedMediaStore
from modex_agent.core.media import Attachment, AttachmentLocator
from modex_agent.input_pipeline.envelope import AttachmentRef, UserInputEnvelope
from modex_agent.input_pipeline.stage import Continue, InputStage, StageResult
from modex_agent.media.gate import perception_gate
from modex_agent.multi_agent.pool_config.media import MediaConfig
from modex_agent.workspace.runtime import bind_workspace_root

logger = logging.getLogger(__name__)

# Only the leading bytes are sniffed; 16 is well past every fixed-offset magic
# the sniffer checks (PNG/JPEG/GIF/PDF/RIFF-WEBP tag at offset 8). Kept small so
# the head read never buffers the whole (potentially 20 MB) file.
_HEAD_BYTES: int = 16


def _read_head_and_size(local_path: Path) -> tuple[bytes, int]:
    """Return the (head bytes, total size) of *local_path*.

    One open: read a capped head for sniffing, then stat for the true size so
    the gate's size cap is checked against the real byte count, not the head.
    """
    with local_path.open("rb") as fh:
        head = fh.read(_HEAD_BYTES)
    size = local_path.stat().st_size
    return head, size


def _relative_to_workspace(stored_path: Path, ws_root: Path) -> str:
    """Render *stored_path* as a forward-slash path relative to *ws_root*.

    ADR §4: ``locator=media`` Attachment ``path`` is relative to the workspace
    root. Falls back to the absolute path when the stored file is not under
    the workspace (defensive — the store resolves under the bound root, so this
    branch is not expected in normal operation).
    """
    try:
        rel = stored_path.resolve().relative_to(ws_root.resolve())
    except ValueError:
        return str(stored_path)
    # Forward slashes keep the record portable across platforms (the transcript
    # is read back on any OS; Windows backslashes would break POSIX readers).
    return rel.as_posix()


class AttachmentIngestStage(InputStage):
    """Gate + persist + classify inbound attachments into Attachment records."""

    async def process(
        self, envelope: UserInputEnvelope, ctx: BotInputContext
    ) -> StageResult:
        # Pure pass-through when there is nothing to ingest OR the media wiring
        # is absent. Legacy envelopes (no attachments) and decision/control
        # envelopes flow through unchanged.
        if not envelope.attachments or ctx.media_store is None:
            return Continue(value=envelope)

        media_store: WorkspaceScopedMediaStore = ctx.media_store
        pool: str = envelope.metadata[RoutingMeta.RESOLVED_POOL]
        # Per-pool MediaConfig (ADR-0013 §7): the context resolves the owning
        # pool's PoolAssemblyDeps.media, falling back to the default instance.
        config = ctx.media_config_for(pool)
        session_id: str = envelope.metadata[RoutingMeta.FULL_SESSION_ID]
        workspace: Path = Path(str(envelope.metadata[RoutingMeta.WORKSPACE]))

        # The store routes writes by the bound workspace root (ctxvar). This
        # stage runs in the input pipeline; bind the envelope's resolved
        # workspace around the save + enforce so bytes land in the right ws.
        with bind_workspace_root(workspace):
            for ref in envelope.attachments:
                record = self._ingest_one(
                    ref, media_store, config, pool, session_id, workspace
                )
                if record is not None:
                    envelope.resolved_attachments.append(record)
        return Continue(value=envelope)

    @staticmethod
    def _ingest_one(
        ref: AttachmentRef,
        media_store: WorkspaceScopedMediaStore,
        config: MediaConfig,
        pool: str,
        session_id: str,
        workspace: Path,
    ) -> Attachment | None:
        """Gate + persist one AttachmentRef. Returns the record, or None.

        None ⇒ the ref had no bytes to read, was rejected by the gate, or the
        producer did not supply a local_path. Rejections are logged at INFO
        (uploader-facing notices are the channel's job; the stage records
        nothing).
        """
        if ref.local_path is None:
            logger.debug("Attachment ref without local_path — skipped")
            return None
        local_path = Path(ref.local_path)
        if not local_path.is_file():
            logger.warning("Attachment local_path missing: %s", local_path)
            return None

        head, size = _read_head_and_size(local_path)
        decision = perception_gate(
            head=head, size=size, filename=ref.filename, config=config
        )
        if not decision.accepted:
            logger.info(
                "Attachment rejected by perception gate: name=%s reason=%s",
                ref.filename,
                decision.reason,
            )
            return None

        attachment_id = uuid.uuid4().hex
        store = media_store.store_for(pool)
        with local_path.open("rb") as stream:
            stored_path = store.save(session_id, attachment_id, stream)
        store.enforce_budget(session_id, config.session_budget_bytes)

        return Attachment(
            id=attachment_id,
            kind=decision.kind,
            # TODO: synthesize a display name from kind+id when filename is None
            #  (G8 renderer)
            name=ref.filename or attachment_id,
            mime=decision.mime,
            size=size,
            path=_relative_to_workspace(stored_path, workspace),
            locator=AttachmentLocator.MEDIA,
        )
