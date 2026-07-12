"""Perception gate — the one accept/reject authority for inbound attachments.

A single pure function decides whether a file may be perceived by the bot at
all (ADR-0013 §7 Layer 1). The same rules govern three consumers:
upload-accept, path-injection (mechanism B), and inline-render (mechanism A) —
so a file that cannot be perceived is refused at ingest, never stored, never
injected, never rendered.

Pure framework: no I/O, no workspace, no pool. The caller supplies the leading
bytes (``head``), the total ``size``, and the optional ``filename`` (extension
fallback only); the gate returns a :class:`GateDecision`.

**Ordering is load-bearing (security).** Disguise rejection runs FIRST, before
type/size, so a PE executable disguised as ``.png`` is rejected as dangerous
regardless of the allowlisted extension — exactly the evasion the dangerous-magic
table defends against (ADR §8).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from modex_agent.media.mime import classify_kind, sniff_mime
from modex_agent.media.models import Kind
from modex_agent.media.security import DANGEROUS_MAGIC
from modex_agent.multi_agent.pool_config.media import MediaConfig


class RejectReason(StrEnum):
    """Why the gate rejected a file.

    The reason is set ONLY when ``accepted`` is False; an accepted decision
    carries ``reason=None``. StrEnum so the value serializes as a stable
    string for logs / uploader-facing notices.
    """

    TYPE_NOT_ALLOWED = "type_not_allowed"
    OVERSIZE = "oversize"
    DANGEROUS_DISGUISE = "dangerous_disguise"


@dataclass(frozen=True)
class GateDecision:
    """Outcome of the perception gate — a frozen value object.

    ``kind`` is always populated (even on reject) so a reject notice can say
    "rejected a KIND file". ``mime`` is the sniffed authoritative MIME; on
    reject it may be None (e.g. a non-sniffed dangerous binary). ``reason`` is
    set only when ``accepted`` is False.
    """

    accepted: bool
    mime: str | None
    kind: Kind
    reason: RejectReason | None


def _is_dangerous_disguise(head: bytes) -> bool:
    """Return True if ``head`` matches any dangerous-executable magic signature.

    The dangerous-magic table is a fixed security policy
    (:data:`DANGEROUS_MAGIC`) — keyed by family, each carrying fixed-offset
    byte signatures. Any match ⇒ dangerous, regardless of declared extension.
    """
    for signatures in DANGEROUS_MAGIC.values():
        for sig in signatures:
            if head.startswith(sig):
                return True
    return False


def perception_gate(
    *,
    head: bytes,
    size: int,
    filename: str | None,
    config: MediaConfig,
) -> GateDecision:
    """Run the perception gate over one file's head + size + (optional) name.

    Steps (order is security-load-bearing):

    1. Sniff authoritative MIME from magic bytes (extension fallback), then
       classify the three-way :class:`Kind`.
    2. **Disguise rejection (first):** dangerous-executable magic ⇒ reject
       :attr:`RejectReason.DANGEROUS_DISGUISE`, no matter the extension.
    3. **Type allow-list:** accept only IMAGE / EXTRACTABLE_DOCUMENT; OTHER ⇒
       reject :attr:`RejectReason.TYPE_NOT_ALLOWED`.
    4. **Per-kind size cap:** IMAGE ≤ ``max_image_bytes``; EXTRACTABLE_DOCUMENT
       ≤ ``max_text_doc_bytes``; over ⇒ reject :attr:`RejectReason.OVERSIZE`.
    5. All pass ⇒ accept, carrying the sniffed MIME + kind.
    """
    mime = sniff_mime(head, filename)
    kind = classify_kind(mime)

    # 2. Disguise rejection first — security. A PE/ELF/Mach-O revealed by magic
    #    is dangerous regardless of the declared extension; reject before any
    #    allow-list logic could let it through.
    if _is_dangerous_disguise(head):
        return GateDecision(
            accepted=False,
            mime=mime,
            kind=kind,
            reason=RejectReason.DANGEROUS_DISGUISE,
        )

    # 3. Type allow-list.
    if kind is Kind.OTHER:
        return GateDecision(
            accepted=False,
            mime=mime,
            kind=kind,
            reason=RejectReason.TYPE_NOT_ALLOWED,
        )

    # 4. Per-kind size cap. OTHER was rejected at step 3, so kind is IMAGE or
    #    EXTRACTABLE_DOCUMENT here — a two-branch ternary covers both.
    cap = config.max_image_bytes if kind is Kind.IMAGE else config.max_text_doc_bytes
    if size > cap:
        return GateDecision(
            accepted=False,
            mime=mime,
            kind=kind,
            reason=RejectReason.OVERSIZE,
        )

    # 5. Accept.
    return GateDecision(accepted=True, mime=mime, kind=kind, reason=None)
