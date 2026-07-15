"""Transcript-scanned attachment id→record resolver (ADR-0013 §11).

The ServerEvent transcript is the single id→path index for attachments — no
separate attachment database. ``find_attachment`` scans one session's
``user_message`` and ``assistant_turn`` events for the record whose ``id``
matches and rebuilds the :class:`Attachment` VO. Inbound records live on
``user_message`` events; outbound records (populated by ``SendFileToUserTool``
in G7) live on ``assistant_turn`` events — both are scanned so the download
endpoint resolves either direction through one call.

Reads accept an optional ``sessions_dir`` override so HTTP handlers (which run
outside any dispatch turn) pass the ``?ws=``-resolved directory explicitly,
mirroring every other read in :class:`WorkspaceScopedTranscriptStore`.
"""

from __future__ import annotations

import functools
import inspect
import logging
from pathlib import Path

from bot.webui.events import ServerEvent
from bot.webui.transcript_store import TranscriptStore
from modex_agent.media.models import Attachment

logger = logging.getLogger(__name__)

# Event types that carry serialized Attachment records. ``user_message`` holds
# inbound records (produced by the ingest stage), ``assistant_turn`` holds
# outbound records (produced by SendFileToUserTool in G7). Scanning both makes
# ``find_attachment`` direction-agnostic.
_ATTACHMENT_EVENTS: frozenset[str] = frozenset({"user_message", "assistant_turn"})


@functools.cache
def _load_accepts_sessions_dir(store_type: type) -> bool:
    """Whether ``store_type.load`` declares a ``sessions_dir`` kwarg.

    The store type is fixed for the server's lifetime, so the signature probe is
    cached once per type instead of re-introspected on every download request.
    """
    return "sessions_dir" in inspect.signature(store_type.load).parameters


async def find_attachment(
    store: TranscriptStore,
    session_id: str,
    attachment_id: str,
    *,
    sessions_dir: Path | None = None,
) -> Attachment | None:
    """Resolve *attachment_id* to its :class:`Attachment` record, or ``None``.

    Scans the session's transcript events for *session_id* (full session
    identifier) in chronological (transcript/append) order and returns the
    first :class:`Attachment` whose ``id`` matches — the oldest match wins.
    Both ``user_message`` (inbound) and ``assistant_turn`` (outbound, populated
    in G7) events are scanned. Returns ``None`` if no record matches.

    *sessions_dir* is forwarded to the store's load path exactly like the HTTP
    read handlers — pass the ``?ws=``-resolved directory for out-of-turn
    callers; omit it for in-turn callers where the store resolves the ctxvar
    root.
    """
    # WorkspaceScopedTranscriptStore.load accepts ``sessions_dir``; the base
    # TranscriptStore.load (and JSONLTranscriptStore.load) does not. Inspect
    # the call site's signature rather than catching TypeError, so a real
    # TypeError raised from inside the iterator is surfaced instead of being
    # silently swallowed and retried as the 1-arg form.
    if _load_accepts_sessions_dir(type(store)):
        events = await store.load(session_id, sessions_dir=sessions_dir)  # type: ignore[call-arg]
    else:
        events = await store.load(session_id)

    for event in events:
        event_type = getattr(event, "event", "")
        if event_type not in _ATTACHMENT_EVENTS:
            continue
        records = _event_attachments(event)
        for record in records:
            if str(record.get("id")) == attachment_id:
                # A corrupted record must not crash the whole scan — skip it
                # and keep looking, mirroring JSONLTranscriptStore.load's
                # per-line skip on bad JSON.
                try:
                    return Attachment.from_dict(record)
                except (KeyError, ValueError, TypeError):
                    logger.warning(
                        "Skipping malformed attachment record (id=%r) while "
                        "resolving %s in session %s.",
                        record.get("id"),
                        attachment_id,
                        session_id,
                    )
                    continue
    return None


def _event_attachments(event: ServerEvent) -> list[dict[str, object]]:
    """Return the serialized attachment list carried on *event*, or empty.

    The field is absent on events written before attachments existed and on
    non-carrying event types; guard both. Uses ``getattr`` only at this
    polymorphic read boundary where events arrive as a heterogeneous list of
    ``ServerEvent`` subclasses (rule 6).
    """
    records = getattr(event, "attachments", None)
    if not isinstance(records, list):
        return []
    return [r for r in records if isinstance(r, dict)]
