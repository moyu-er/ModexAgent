"""Media configuration value object."""

from __future__ import annotations

from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Byte-unit constants — local to this module so the value object stays portable.
# ---------------------------------------------------------------------------
_MB: int = 1024 * 1024
_GB: int = 1024 * _MB


@dataclass(frozen=True)
class MediaConfig:
    """Attachment perception-gate + storage-budget configuration.

    Carries the single source of truth for the size caps and the per-session
    budget shared by upload-accept, path-injection, and inline-render
    (ADR-0013 §7). The dangerous-executable deny-list is a fixed security
    policy owned by :mod:`modex_agent.media.security`, not a tunable field
    here — a caller must not be able to disable disguise-rejection.

    Frozen value object — overrides are a new instance, not in-place mutation.
    Defaults: image 20 MB, text/doc 10 MB, session budget 500 MB, outbound
    cap 1 GB.
    """

    max_image_bytes: int = 20 * _MB
    max_text_doc_bytes: int = 10 * _MB
    session_budget_bytes: int = 500 * _MB
    max_outbound_bytes: int = _GB
