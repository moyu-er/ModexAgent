"""System-prompt renderer for the external coding agent harness.

`render_system_prompt` builds the plain-text string the harness passes
to the provider CLI via ``--append-system-prompt`` (Pi) or the
equivalent flag (OpenCode). The content is provider-agnostic: it tells
the external agent (a) it is running inside ModexAgent, (b) how to send
a message to another agent via ``modexctl send``, (c) that raw stdout is
streamed to the orchestrator but NOT delivered as a message, and (d)
the routable targets table derived from ``MODEX_TARGETS``.

The renderer is a pure function over the targets list — no I/O, no
state. Targets are the ``(name, description)`` pairs the
``CommunicationTargetStore`` already produces in a stable order; the
renderer preserves that order so prompt diffs stay deterministic.
"""

from __future__ import annotations

__all__ = ["render_system_prompt"]


def render_system_prompt(targets: list[tuple[str, str]]) -> str:
    """Render the ``--append-system-prompt`` string from the targets list.

    Args:
        targets: Order-preserving list of ``(name, description)`` pairs
            sourced from ``MODEX_TARGETS`` (via
            :class:`ExternalEnvSpec.targets`). May be empty — the
            targets table is omitted in that case but the communication
            instructions remain.

    Returns:
        A plain-text (Markdown-flavoured) string suitable for passing
        verbatim as the provider CLI's append-system-prompt argument.
        No trailing newline is appended so callers can concatenate or
        wrap freely.
    """
    lines: list[str] = [
        "You are running as an external coding agent integrated with ModexAgent.",
        "",
        "## Inter-agent communication",
        (
            "Use `modexctl send --to <name> --content <text>` to send a "
            "message to another agent. This is the ONLY way your output "
            "reaches another agent."
        ),
        (
            "Your stdout is streamed to the orchestrator for observability "
            "but is NOT delivered as a message. For any output that must "
            "reach another agent, call `modexctl send` explicitly."
        ),
        "Run `modexctl agents` to list routable targets at any time.",
        "",
        "## Framework-managed files",
        (
            "The `.modex/` directory is framework-managed internal state. "
            "Do NOT read, modify, or delete anything under `.modex/`."
        ),
    ]

    if targets:
        lines.append("")
        lines.append("## Routable targets")
        lines.append("| Name | Description |")
        lines.append("|------|-------------|")
        for name, description in targets:
            # Keep name/description verbatim — the targets list is already
            # validated upstream. A description containing a pipe would
            # break the Markdown table cell, but that is a caller concern
            # (the CommunicationTargetStore controls description content).
            lines.append(f"| {name} | {description} |")

    return "\n".join(lines)
