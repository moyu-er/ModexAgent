"""Sandbox guard presentation layer — verdict→copy mapping helpers.

Owns denial wording, exact-call approval-marker matching, container-death
signatures, and denial-context translation. Marker matching and delegated
judgments may access the filesystem through canonical path resolution.
``SandboxGuardInterceptor`` owns the interception flow.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from modex_agent.interceptor.abc import ToolCallContext
from modex_agent.sandbox.decision import (
    GuardCategory,
    GuardVerdict,
)
from modex_agent.sandbox.settings import SandboxPolicy
from modex_agent.sandbox.tool_matrix import (
    ToolEffect,
    approval_anchor,
    describe_tool_security,
)

if TYPE_CHECKING:
    from modex_agent.sandbox.decision import SecurityDecisionService

__all__ = [
    "translate_denial",
]

_DENIED_PREFIX = "Sandbox policy denied"

# docker/podman CLI feature for a vanished container (daemon restart /
# OOM kill). This signals uncertainty, never permission to replay a command.
CONTAINER_DEAD_MARKERS: tuple[str, ...] = (
    "no such container",
    "container not found",
    "is not running",
)

_HARD_LABEL: dict[GuardCategory, str] = {
    GuardCategory.DENY_RULE: "a hard policy rule hit",
    GuardCategory.TRAVERSAL: "a path traversal finding",
    GuardCategory.SSRF: "a private/internal network target",
}


def denied(reason: str) -> str:
    """The uniform denial copy prefix the tool layer reports."""
    return f"{_DENIED_PREFIX}: {reason}"


def _policy_clause(policy: SandboxPolicy) -> str:
    return f"policy: {policy.value}"


def _is_write_tool(tool_name: str) -> bool:
    """Whether the tool-effect seam classifies ``tool_name`` as WRITE."""
    return describe_tool_security(tool_name).effect is ToolEffect.WRITE


def _join_sections(*sections: str) -> str:
    """Join nonempty copy blocks with one blank line between them."""
    return "\n\n".join(section for section in sections if section)


def _target_section(target: str | None) -> str:
    """The attempted target, indented on its own line."""
    return f"  target: {target}" if target else ""


def _roots_section(roots: tuple[Path, ...]) -> str:
    """The allowed envelope — one root per line."""
    if not roots:
        return ""
    return "Allowed roots:\n" + "\n".join(f"  - {root}" for root in roots)


def evaluate_call(decision: SecurityDecisionService, call: ToolCallContext) -> GuardVerdict:
    """Judge one intercepted call via the service's typed tool-effect seam.

    The seam dispatches on the tool's descriptor (effect + declared
    target argument); this layer only words the outcome. Unclassified
    tools pass clean — approval/tool policy apply unchanged.
    """
    return decision.evaluate_tool_call(call.tool_name, dict(call.arguments))


def verdict_to_denial(
    verdict: GuardVerdict,
    policy: SandboxPolicy,
    workspace_root: str,
    tool_name: str,
    target: str | None,
) -> str | None:
    """Map a non-clean verdict to the English denial copy (None when clean).

    Structured blocks separated by blank lines: the header (category +
    policy), the attempted target (one line), the guard fact
    (``verdict.reason`` verbatim), the allowed roots (one root per
    line), and the actionable hint. The service carries the source fact;
    this layer adds policy context and presentation.

    For file-tool boundary findings the raw target line is omitted: the
    fact sentence already names the canonical resolved path, and the
    raw argument usually reads identically — repeating it would state
    the same path twice.
    """
    match verdict.category:
        case GuardCategory.CLEAN:
            return None
        case GuardCategory.BOUNDARY:
            effect = describe_tool_security(tool_name).effect
            show_target = (
                bool(target) and effect not in (ToolEffect.READ, ToolEffect.WRITE)
            )
            return _join_sections(
                denied(f"target outside the sandbox boundary ({_policy_clause(policy)})"),
                _target_section(target if show_target else None),
                verdict.reason or "",
                _roots_section(verdict.allowed_roots or (Path(workspace_root),)),
                "Use paths within the allowed roots, or adjust writable_roots.",
            )
        case GuardCategory.DENY_RULE if _is_write_tool(tool_name) and (
            policy is SandboxPolicy.READ_ONLY
        ):
            return _join_sections(
                denied(
                    f"write tool '{tool_name}' refused — {_policy_clause(policy)} "
                    "(every path is read-only)"
                ),
                _target_section(target),
                "Request a sandbox policy change to enable writes.",
            )
        case GuardCategory.DENY_RULE | GuardCategory.TRAVERSAL | GuardCategory.SSRF:
            return _join_sections(
                denied(
                    f"{_HARD_LABEL[verdict.category]} ({_policy_clause(policy)})"
                ),
                _target_section(target),
                verdict.reason or "the call hit a deny rule",
            )


def is_container_dead(error: str) -> bool:
    """Whether an executed-result error carries a container-death feature."""
    lowered = error.lower()
    return any(marker in lowered for marker in CONTAINER_DEAD_MARKERS)


def anchor_matches_approval(
    call_id: str,
    tool_name: str,
    arguments: dict,
    workspace_root: Path,
    marked: object,
) -> bool:
    """Whether the human-approved marker waives THIS exact call.

    ``marked`` is the turn-state value under
    ``TurnCustomKey.HUMAN_APPROVED_CALLS`` (``tool_call_id → anchor``,
    written by ``ApprovalResumer`` on the ALLOW decision). The anchor is
    re-derived from the live arguments + live root via the SAME
    ``approval_anchor`` function the resumer used. A match binds only the
    declared target identity, not dynamic shell effects. Root or argument
    changes that alter the stored anchor prevent a boundary waiver.
    """
    if not call_id or not isinstance(marked, dict):
        return False
    approved = marked.get(call_id)
    if not isinstance(approved, str):
        return False
    return approval_anchor(tool_name, arguments, workspace_root) == approved


def sandbox_restarted_error_text(original_error: str) -> str:
    """Transport failure is not proof that the target command never ran."""
    return (
        "Sandbox execution is uncertain; the command was not replayed. "
        "Inspect its side effects before retrying. Restore the selected container "
        "or recreate the agent shell before submitting another command. "
        f"Original error: {original_error}"
    )


def translate_denial(stderr: str, policy: SandboxPolicy) -> str:
    """Translate a container-emitted denial feature into actionable copy.

    Pure function for the tool layer: when executed commands fail with
    ``Read-only file system`` / ``Operation not permitted`` (EPERM), the
    raw kernel wording says nothing about WHICH policy caused it. This
    appends the sandbox-policy context and a rewrite direction. Clean
    stderr passes through unchanged — translation is additive only.
    """
    lowered = stderr.lower()
    hit = "read-only file system" in lowered or "operation not permitted" in lowered
    if not hit:
        return stderr
    if "read-only file system" in lowered:
        action = (
            "the path is read-only inside the sandbox boundary; write within "
            "a writable root, or request a sandbox policy change"
        )
    else:
        action = (
            "the operation was rejected by the sandbox boundary; run it within "
            "the allowed roots, or request a sandbox policy change"
        )
    return f"{stderr}\n[modex sandbox] ({_policy_clause(policy)}) {action}."
