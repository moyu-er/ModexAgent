"""History application logic — pure projection from raw message dicts and
materialized transcript turns.

Owns the eight-field Server Projection (``HistoryMessage``) and the
newest-first ordering + limit. Pure functions over inputs — no I/O, no
side effects, easily testable in isolation.

The legacy ``modexctl.history`` module (deleted in T10) was the prior art
for the ``filter_vo`` + ``format_jsonl`` pair; this module is the
server-side equivalent. The CLI applies its OWN independent eight-field
Client Output Projection (T04 §3) so the two surfaces can diverge if the
server adds fields later.

``project_transcript_history`` (T05) projects materialized transcript turns
for external coding agent sessions (Pi, OpenCode). It follows Source
Fidelity (D21): ``message_id``, ``tool_call_id``, and ``tool_calls`` are
never fabricated — transcript-derived records omit them entirely.
"""

from __future__ import annotations

from typing import Any

from bot.control.models import HistoryMessage
from bot.webui.transcript_store import MaterializedTurn

#: Eight-field Server Projection allowlist. Originally matched the legacy
#: ``_HISTORY_VO_FIELDS`` from the now-deleted ``modexctl.history`` module
#: (T10) so the server and the CLI produced identical field sets; the two
#: surfaces are now free to diverge. Internal markers (``_deleted``,
#: ``_pinned``, ``token_count``, ``is_content_json``, ``content_format``,
#: ``reasoning_content``) are stripped.
_HISTORY_FIELDS: frozenset[str] = frozenset(
    {
        "role",
        "content",
        "tool_calls",
        "tool_call_id",
        "tool_name",
        "name",
        "created_at",
        "message_id",
    }
)


def _filter_to_history_fields(raw: dict[str, Any]) -> dict[str, Any]:
    """Keep only the eight history fields from a raw message dict.

    ``content`` is preserved verbatim — ``str`` stays ``str``, ``list``
    (multimodal ContentPart) stays ``list``. Missing fields are simply
    absent from the result; :class:`HistoryMessage` defaults them to ``None``.

    ``created_at`` is normalised to ``str`` because SQLite stores it as int
    milliseconds (ADR-0029) while :class:`HistoryMessage` declares it as
    ``str | None``. An int is converted via ``str(int)``; other types pass
    through unchanged.
    """
    out: dict[str, Any] = {}
    for k, v in raw.items():
        if k not in _HISTORY_FIELDS:
            continue
        if k == "created_at" and isinstance(v, int):
            out[k] = str(v)
        else:
            out[k] = v
    return out


def _created_at_sort_key(msg: dict[str, Any]) -> int:
    """Return a numeric sort key for ``created_at`` (descending order).

    SQLite stores ``created_at`` as int milliseconds (ADR-0029). Missing
    or non-numeric values sort as 0 (ancient) so they land at the end of
    a descending-order list — matching SQLite's NULLS-LAST behaviour for
    ``ORDER BY created_at DESC``.
    """
    value = msg.get("created_at")
    if value is None:
        return 0
    if isinstance(value, int):
        return value
    # Pydantic / file-backed stores may carry ISO strings or numeric strings.
    try:
        return int(value)
    except (ValueError, TypeError):
        return 0


def project_history_messages(
    raw_messages: list[dict[str, Any]],
    limit: int,
) -> list[HistoryMessage]:
    """Project raw message dicts to :class:`HistoryMessage`, newest-first.

    Steps:
        1. Sort raw dicts by ``created_at`` descending (newest first).
           Messages with no ``created_at`` sort last.
        2. Take the first ``limit``.
        3. Filter each to the eight-field allowlist and validate via
           :class:`HistoryMessage`.

    ``limit`` is the already-validated request limit (1-10). No additional
    clamping is performed here — the caller (facade) passes the Pydantic-
    validated value, and the CLI clamps before sending.
    """
    # Sort a copy so the caller's list is not mutated.
    ordered = sorted(raw_messages, key=_created_at_sort_key, reverse=True)
    capped = ordered[:limit]
    return [HistoryMessage.model_validate(_filter_to_history_fields(m)) for m in capped]


def project_transcript_history(
    turns: list[MaterializedTurn],
    limit: int,
) -> list[HistoryMessage]:
    """Project materialized transcript turns to :class:`HistoryMessage`.

    Used by :meth:`BotControlFacade.history` when
    ``execution_strategy == EXTERNAL_CODING`` (T05). The transcript path
    reuses :func:`bot.webui.transcript_store._materialize_events` for
    grouping/coalescing/pairing, then projects each materialized block to
    a logical :class:`HistoryMessage` record following Source Fidelity (D21):

    - ``text`` block → ``role=assistant, content=text``
    - ``tool`` block → ``role=tool, content=result, tool_name=tool``
      (already paired by ``_materialize_events`` via ``call_id``)
    - ``reasoning`` block → **discarded** (not in the 8-field CLI output)
    - unknown block kinds → **discarded** (no representable CLI history record)

    ``message_id``, ``tool_call_id``, ``tool_calls``, and ``name`` are never
    fabricated — they stay ``None`` and are omitted at serialization time via
    ``exclude_none=True``. ``created_at`` is set from the turn's
    ``started_at`` (int ms → ``str``), matching the native path's convention.

    Ordering: logical records are sorted newest-first by ``started_at``
    (descending). The sort is stable, so records from the same turn preserve
    their within-turn block order (text before tool, as emitted).

    ``limit`` is applied to the **logical records** list — never to raw
    events. If multiple records come from the same turn, they count
    individually toward the limit.
    """
    records: list[tuple[int, HistoryMessage]] = []
    for turn in turns:
        started_at = turn.started_at
        created_at = str(started_at) if started_at else None
        for block in turn.blocks:
            kind = str(block.get("kind", ""))
            if kind == "text":
                text = block.get("text")
                records.append(
                    (
                        started_at,
                        HistoryMessage(
                            role="assistant",
                            content=str(text) if text is not None else None,
                            created_at=created_at,
                        ),
                    )
                )
            elif kind == "tool":
                result = block.get("result")
                tool = block.get("tool")
                records.append(
                    (
                        started_at,
                        HistoryMessage(
                            role="tool",
                            content=str(result) if result is not None else None,
                            tool_name=str(tool) if tool else None,
                            created_at=created_at,
                        ),
                    )
                )
            # reasoning and other kinds are discarded — no representable
            # CLI history record (Source Fidelity D21).

    # Newest-first; stable sort preserves within-turn block order.
    records.sort(key=lambda pair: pair[0], reverse=True)
    return [msg for _, msg in records[:limit]]
