"""AGENTS.md marker-block writer for the external coding agent runtime.

The harness writes a marker-delimited block into ``<workdir>/AGENTS.md``
so the external coding agent (Pi / OpenCode / etc.) sees static runtime
notes — the communication contract, the modexbot CLI usage, the session
layout — alongside any user-authored content in the same file.

The block is idempotent: re-writing replaces only the content between
the markers, preserving everything outside them verbatim. This lets the
harness refresh the runtime block every turn without clobbering the
user's hand-written project notes.

Marker shape::

    <!-- BEGIN MODEX-RUNTIME (auto-managed; do not edit) -->
    ...static content ( regenerated every write )...
    <!-- END MODEX-RUNTIME -->

The writer is a pure function over a :class:`pathlib.Path` and an
optional content string; the default content comes from
:func:`default_runtime_block`.
"""

from __future__ import annotations

import re
from pathlib import Path

from modex_agent.core.agent import AgentCommKind

__all__ = [
    "BEGIN_MARKER",
    "END_MARKER",
    "default_runtime_block",
    "write_runtime_block",
    "read_runtime_block",
]

BEGIN_MARKER = "<!-- BEGIN MODEX-RUNTIME (auto-managed; do not edit) -->"
END_MARKER = "<!-- END MODEX-RUNTIME -->"

# Matches the marker block (inclusive) across newlines. ``[\s\S]`` matches
# any character including newlines without relying on ``re.DOTALL`` on the
# whole pattern (we want ``^``/``$` to stay line-anchored for clarity).
_BLOCK_RE = re.compile(
    re.escape(BEGIN_MARKER) + r"[^\n]*\n[\s\S]*?" + re.escape(END_MARKER),
)


def default_runtime_block(
    comm_kind: AgentCommKind = AgentCommKind.NORMAL,
) -> str:
    """Return the default static runtime-notes content.

    The content is provider-agnostic and intentionally short: the
    external agent's system prompt (see :mod:`system_prompt`) carries
    the dynamic targets table; AGENTS.md carries the stable contract
    that does not change per turn.

    Args:
        comm_kind: Routing kind. ``NORMAL`` (peer/main agent) instructs
            the agent to use ``modexctl send`` for inter-agent output.
            ``SUBAGENT`` instructs the agent that its final reply is
            forwarded to its caller automatically and ``modexctl send``
            is only for questions/decisions.
    """
    if comm_kind is AgentCommKind.SUBAGENT:
        return "\n".join(
            [
                "## ModexAgent runtime",
                "",
                "You are running as a subagent inside ModexAgent.",
                "",
                "Your final reply is your deliverable — it is forwarded to your caller "
                "automatically when your turn ends. Output your result in your reply text.",
                "",
                "Use `modexctl send --to <name> --content <text>` only to ask a question "
                "or request a decision when you cannot proceed without input.",
                "",
                "- The `.modex/` directory is framework-managed internal state. "
                "Do NOT read, modify, or delete anything under `.modex/`.",
            ]
        )
    return "\n".join(
        [
            "## ModexAgent runtime",
            "",
            "You are running inside ModexAgent as an external coding agent.",
            "",
            "- `modexctl send --to <name> --content <text>` sends a message to another agent.",
            "- Run `modexctl agents` to list routable targets at any time.",
            "- The `.modex/` directory is framework-managed internal state. "
            "Do NOT read, modify, or delete anything under `.modex/`.",
        ]
    )


def write_runtime_block(
    path: Path,
    content: str | None = None,
) -> None:
    """Idempotently write (or replace) the MODEX-RUNTIME block in ``path``.

    Behaviour:

    - If ``path`` does not exist, it is created with the block as its
      sole content (plus a trailing newline).
    - If ``path`` exists and already contains a marker block, ONLY the
      block is replaced; all surrounding user content is preserved
      byte-for-byte.
    - If ``path`` exists but has no marker block, the block is appended
      at the end (preceded by a blank line when the file is non-empty
      and does not already end in one).

    Args:
        path: Path to the AGENTS.md file (typically
            :attr:`ExternalPaths.agents_md`).
        content: Optional block body. Defaults to
            :func:`default_runtime_block`.
    """
    body = content if content is not None else default_runtime_block()
    block = f"{BEGIN_MARKER}\n{body}\n{END_MARKER}"

    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(block + "\n", encoding="utf-8")
        return

    existing = path.read_text(encoding="utf-8")

    if BEGIN_MARKER in existing:
        # Replace the existing block (and any trailing chars up to the
        # END_MARKER) in place. Everything outside the match is
        # preserved verbatim.
        new_text = _BLOCK_RE.sub(block, existing)
        path.write_text(new_text, encoding="utf-8")
        return

    # No existing block — append. Ensure a separating blank line when
    # the file has content that does not already end in a newline-gap.
    separator = (
        ""
        if existing == "" or existing.endswith("\n\n")
        else ("\n" if existing.endswith("\n") else "\n\n")
    )
    path.write_text(existing + separator + block + "\n", encoding="utf-8")


def read_runtime_block(path: Path) -> str | None:
    """Return the body of the MODEX-RUNTIME block in ``path``, or ``None``.

    The body is the text strictly between ``BEGIN_MARKER`` and
    ``END_MARKER`` (markers excluded, surrounding newlines trimmed).
    Returns ``None`` when the file is absent or contains no block.
    """
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8")
    match = _BLOCK_RE.search(text)
    if match is None:
        return None
    matched = match.group(0)
    # Strip the begin marker line and the end marker line.
    inner = matched[len(BEGIN_MARKER) :]
    if inner.endswith(END_MARKER):
        inner = inner[: -len(END_MARKER)]
    # Drop the leading newline left after removing BEGIN_MARKER and the
    # trailing newline before END_MARKER.
    return inner.strip("\n")
