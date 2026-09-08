"""Experience-package constants and path helpers (plan §10.2/§10.3)."""

from __future__ import annotations

from pathlib import Path
from typing import Final

#: The experience document's canonical filename inside each directory.
EXPERIENCE_FILENAME: Final = "EXPERIENCE.md"

#: Per-experience metadata sidecar filename (inside each experience dir).
META_FILENAME: Final = ".exp.meta.json"

#: Cap on experiences injected into one system prompt (anti-bloat).
MAX_INJECTED_EXPERIENCES: Final = 20

#: Default LRU eviction cap for the curator.
DEFAULT_MAX_EXPERIENCES: Final = 20

#: Default curator cadence: once per day, in seconds.
DEFAULT_CURATOR_INTERVAL: Final = 86400

#: The injection section's id (single source for contribute + bind).
INJECTION_SECTION_ID: Final = "experience.injection"

#: The reviewer trace subdirectory (never inside the experience data).
REVIEW_TRACES_DIRNAME: Final = "review_traces"


def review_traces_dir(experience_dir: Path) -> Path:
    """The reviewer's trace directory for an experience root.

    Sibling of the experience dir (``experience_dir.parent /
    "review_traces"``) so traces never pollute the experience data.
    """
    return experience_dir.parent / REVIEW_TRACES_DIRNAME
