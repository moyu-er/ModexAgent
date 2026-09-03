"""inject_multimodal — resolve persisted ``media://`` parts at the LLM boundary.

The message history persists ``media://<attachment_id>`` references (never
base64); the bytes live in the ``MediaStore`` (uploads/reads subtrees). At
each LLM call — after governance, before the provider — this module walks
the LLM-bound message list and resolves every reference into a data-URL
``ImageUrlPart`` on a copy-on-write basis:

- **Modality gate**: a part whose modality the active model does not support
  is dropped with ERROR (a reference persisted for a vision model never
  reaches a text-only model's wire).
- **Budget two-pass**: all ``media://`` parts are collected and measured
  first; over ``_MAX_INJECTED_MEDIA_COUNT`` parts or
  ``_MAX_INJECTED_MEDIA_BYTES`` decoded bytes, the OLDEST (first in message
  order) are offloaded to ``[media offloaded: <aid>]`` text placeholders
  with ERROR.
- **Placeholder degradation**: no store wired, missing bytes, or corrupt
  bytes degrade to ``[media unavailable: <aid>]`` text placeholders with
  ERROR — a broken image never fails the LLM call.

Resolved data URLs are cached per ``(id(store), session_id, attachment_id)``
(weakref-guarded against store id() reuse) so repeated iterations within and
across turns do not re-read and re-encode stored bytes.
"""

from __future__ import annotations

import base64
import logging
import weakref
from typing import Any

from modex_agent.core.agent import AgentContext
from modex_agent.core.capabilities import ModelCapabilities
from modex_agent.core.constants import _MAX_INJECTED_MEDIA_BYTES, _MAX_INJECTED_MEDIA_COUNT
from modex_agent.core.media import MediaStore
from modex_agent.core.message import (
    ChatMessage,
    ContentPart,
    ImageUrl,
    ImageUrlPart,
    TextPart,
    content_part_modality,
    parse_media_ref,
)
from modex_agent.media.media_utils import compress_image
from modex_agent.media.mime import sniff_mime

logger = logging.getLogger(__name__)

__all__ = ["inject_multimodal"]

_PLACEHOLDER_UNAVAILABLE = "[media unavailable: {aid}]"
_PLACEHOLDER_OFFLOADED = "[media offloaded: {aid}]"

# (id(store), session_id, attachment_id) -> (weakref to store, (data URL, size)).
# The weakref guard makes a stale entry inert when a store is garbage
# collected and a new store reuses its id(); the cache is cleared wholesale
# at the size cap to stay bounded.
_RESOLVED_URL_CACHE: dict[tuple[int, str, str], tuple[weakref.ref, tuple[str, int]]] = {}
_RESOLVED_URL_CACHE_MAX = 128


def _resolve_media(store: MediaStore, session_id: str, aid: str) -> tuple[str, int] | None:
    """Resolve one ``media://`` reference to ``(data URL, decoded size)``; cached.

    ``None`` (with ERROR) when the store has no bytes for the reference, the
    reference collides across subtrees, or the bytes are not decodable.
    """
    cache_key = (id(store), session_id, aid)
    cached = _RESOLVED_URL_CACHE.get(cache_key)
    if cached is not None and cached[0]() is store:
        return cached[1]
    try:
        data = store.resolve_bytes(session_id, aid)
    except Exception as exc:  # MediaRefCollisionError or store I/O failure
        logger.error("media injection: resolving media://%s failed: %s", aid, exc)
        return None
    if data is None:
        logger.error(
            "media injection: no stored bytes for media://%s (session %s)", aid, session_id
        )
        return None
    compressed = compress_image(data, sniff_mime(data) or "image/png")
    if compressed is None:
        logger.error("media injection: stored bytes for media://%s are not decodable", aid)
        return None
    resolved = (
        f"data:{compressed.media_type};base64,{base64.b64encode(compressed.data).decode()}",
        len(compressed.data),
    )
    if len(_RESOLVED_URL_CACHE) >= _RESOLVED_URL_CACHE_MAX:
        _RESOLVED_URL_CACHE.clear()
    _RESOLVED_URL_CACHE[cache_key] = (weakref.ref(store), resolved)
    return resolved


def inject_multimodal(messages: list[ChatMessage], ctx: AgentContext) -> list[ChatMessage]:
    """Resolve ``media://`` parts in the LLM-bound message list (copy-on-write).

    Messages whose content is not a parts list pass through as the same
    objects; a message is replaced only when one of its parts is dropped,
    resolved, offloaded, or degraded. The input messages are never mutated —
    the persisted history keeps its references.
    """
    if not any(isinstance(m.content, list) for m in messages):
        return messages

    if ctx.runtime is not None and ctx.runtime.model_info is not None:
        caps = ctx.runtime.model_info.capabilities
    else:
        # No model info on the runtime: assume the ModelCapabilities default
        # (TEXT-only) — text parts always survive, image parts gate out.
        caps = ModelCapabilities()
    store = ctx.runtime.services.media_store if ctx.runtime is not None else None
    session_id = str(ctx.session)

    # Pass 1 — plan every parts message: one stable entry per original part.
    # Entry kinds: "keep" (passthrough part), "drop" (modality-gated),
    # "unavailable" (placeholder, carries the aid), "resolve" (carries
    # (aid, data URL)). Candidates record resolvable positions in message
    # order for the budget pass.
    plans: list[list[tuple[str, Any]] | None] = [None] * len(messages)
    candidates: list[tuple[int, int, str, str, int]] = []
    for mi, msg in enumerate(messages):
        if not isinstance(msg.content, list):
            continue
        plan: list[tuple[str, Any]] = []
        for pi, part in enumerate(msg.content):
            modality = content_part_modality(part)
            if not caps.supports(modality):
                logger.error(
                    "media injection: %s part unsupported by the active model, dropped "
                    "(message %d, part %d)",
                    modality,
                    mi,
                    pi,
                )
                plan.append(("drop", None))
                continue
            aid = parse_media_ref(part.image_url.url) if isinstance(part, ImageUrlPart) else None
            if aid is None:
                plan.append(("keep", part))
                continue
            if store is None:
                logger.error(
                    "media injection: no media store wired, media://%s not resolvable", aid
                )
                plan.append(("unavailable", aid))
                continue
            resolved = _resolve_media(store, session_id, aid)
            if resolved is None:
                plan.append(("unavailable", aid))
                continue
            url, size = resolved
            plan.append(("resolve", (aid, url)))
            candidates.append((mi, pi, aid, url, size))
        plans[mi] = plan

    # Pass 2 — budget: offload OLDEST-first (message order) until within both
    # the count and the decoded-bytes ceilings.
    count = len(candidates)
    total_bytes = sum(size for *_, size in candidates)
    offload_positions: set[tuple[int, int]] = set()
    head = 0
    while (
        count > _MAX_INJECTED_MEDIA_COUNT or total_bytes > _MAX_INJECTED_MEDIA_BYTES
    ) and head < len(candidates):
        mi, pi, aid, _url, size = candidates[head]
        logger.error(
            "media injection: media budget exceeded, media://%s offloaded (oldest-first)", aid
        )
        offload_positions.add((mi, pi))
        count -= 1
        total_bytes -= size
        head += 1

    # Pass 3 — assemble: rebuild only the messages whose plan is not pure
    # "keep"; every other message object passes through untouched.
    out: list[ChatMessage] | None = None
    for mi, msg in enumerate(messages):
        plan = plans[mi]
        if plan is None:
            continue
        new_parts: list[ContentPart] = []
        replaced = False
        for pi, (kind, value) in enumerate(plan):
            if kind == "keep":
                new_parts.append(value)
            elif kind == "drop":
                replaced = True
            elif kind == "unavailable":
                new_parts.append(TextPart(text=_PLACEHOLDER_UNAVAILABLE.format(aid=value)))
                replaced = True
            else:
                aid, url = value
                if (mi, pi) in offload_positions:
                    new_parts.append(TextPart(text=_PLACEHOLDER_OFFLOADED.format(aid=aid)))
                else:
                    new_parts.append(ImageUrlPart(image_url=ImageUrl(url=url)))
                replaced = True
        if replaced:
            if out is None:
                out = list(messages)
            out[mi] = msg.model_copy(update={"content": new_parts})
    return out if out is not None else messages
