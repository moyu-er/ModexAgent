"""Typed tool-effect seam — the single security vocabulary for tool names.

One deep module owns what the security layers need to know about a tool
name: which effect a call has (read / write / execute / execution-input
/ web / none) and which argument carries the claimable target (a path,
a command, a URL). ``SecurityDecisionService``, ``SecurityClassifier``,
``SandboxGuardInterceptor`` (via ``guard_presentation``), and the
human-approval anchor all consume this seam — one source, every call
point, no copied name sets.

Why a central catalog instead of Tool-object metadata: the approval
classifier and the interceptor judge calls by NAME from ``ToolCall``
records before any ``Tool`` instance exists at the call site, so the
vocabulary lives here as declarative data (the ``Tool`` ABC stays
source-compatible and metadata-free). ``ToolSecurityDescriptor`` is the
cross-module contract; unknown names (MCP, custom tools) get the NONE
default — no path claim, and existing approval/tool policy still
applies unchanged.

``approval_anchor`` derives
the stable identity of one tool call from the card-shown arguments: for
file-effect tools the path resolved against the workspace root via the
canonical boundary seam (expanduser → anchor → resolve; symlinked
targets anchor to their resolved location), for ``bash`` the command
string verbatim (also stdin input), and for ``web_reader`` the URL verbatim.
Unknown tools have no anchor. Only schemas with a workspace-default path
get a default target; absent required file paths never invent approval targets.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from pathlib import Path
from typing import assert_never

from pydantic import BaseModel, ConfigDict, model_validator

from modex_agent.workspace.boundary import (
    PathCanonicalizationError,
    canonicalize_path,
)

__all__ = [
    "PermissionClass",
    "ToolCallTarget",
    "ToolEffect",
    "ToolSecurityDescriptor",
    "approval_anchor",
    "describe_tool_security",
    "extract_call_target",
]


class PermissionClass(StrEnum):
    """The two-class permission model every catalogued tool belongs to.

    ``PARALLEL`` is the read-only family — unrestricted by default,
    narrowable per tool. ``EXCLUSIVE`` is the read-write family —
    bounded by the declared write surface; bash-class members are
    bounded by the kernel substrate and command-text guards instead of
    path arguments. Uncatalogued tools (effect NONE) belong to neither
    class and claim no boundary coverage.
    """

    PARALLEL = "parallel"
    EXCLUSIVE = "exclusive"


class ToolEffect(StrEnum):
    """What kind of sandbox-relevant effect one tool call has."""

    READ = "read"  # File reads, listings, searches: known-path boundary checks
    WRITE = "write"  # File writes/edits: READ_ONLY refusal and path boundary
    EXECUTE = "execute"  # Command execution: best-effort command guards
    EXECUTION_INPUT = "execution_input"  # stdin: same best-effort checks as commands
    WEB = "web"  # URL target: static SSRF checks
    NONE = "none"  # Unknown/MCP/no declared boundary effect: no target claim


class ToolCallTarget(BaseModel):
    """The claimable target extracted from one call's raw arguments.

    Extraction sets one effect target. A command approval anchor may also
    bind an explicit working directory using this typed serializer. A
    required missing/empty argument or a non-string value yields ``None``.
    Optional workspace-default paths resolve to the descriptor's default.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str | None = None
    command: str | None = None
    url: str | None = None


_EFFECTS_CLAIMING_TARGET: frozenset[ToolEffect] = frozenset(
    {ToolEffect.READ, ToolEffect.WRITE, ToolEffect.EXECUTE, ToolEffect.EXECUTION_INPUT, ToolEffect.WEB}
)


class ToolSecurityDescriptor(BaseModel):
    """The security vocabulary entry for one tool name.

    ``target_argument`` is declarative data naming the schema argument
    that carries the claimable target (``path`` for today's file tools,
    ``command`` for bash, ``url`` for web_reader — whatever the tool's
    actual schema declares). Input effects declare ``line`` / ``data``;
    NONE claims no target. ``default_target`` is only for optional paths.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    effect: ToolEffect = ToolEffect.NONE
    target_argument: str | None = None
    default_target: str | None = None

    @property
    def permission_class(self) -> PermissionClass | None:
        """The permission class this descriptor belongs to.

        Derived from the effect — READ/WEB are the parallel (read-only)
        family, WRITE/EXECUTE/EXECUTION_INPUT the exclusive (read-write)
        family. ``None`` for unclassified tools: outside the model.
        """
        match self.effect:
            case ToolEffect.READ | ToolEffect.WEB:
                return PermissionClass.PARALLEL
            case ToolEffect.WRITE | ToolEffect.EXECUTE | ToolEffect.EXECUTION_INPUT:
                return PermissionClass.EXCLUSIVE
            case ToolEffect.NONE:
                return None
            case unreachable:
                assert_never(unreachable)

    @model_validator(mode="after")
    def _claiming_effect_declares_target(self) -> ToolSecurityDescriptor:
        if self.effect in _EFFECTS_CLAIMING_TARGET and not self.target_argument:
            raise ValueError(f"effect {self.effect} must declare target_argument")
        return self


_DESCRIPTORS: dict[str, ToolSecurityDescriptor] = {
    # File-effect tools (target argument per each tool's declared schema).
    "read": ToolSecurityDescriptor(effect=ToolEffect.READ, target_argument="path"),
    "ls": ToolSecurityDescriptor(effect=ToolEffect.READ, target_argument="path", default_target="."),
    "glob": ToolSecurityDescriptor(effect=ToolEffect.READ, target_argument="path", default_target="."),
    "grep": ToolSecurityDescriptor(effect=ToolEffect.READ, target_argument="path", default_target="."),
    "ast_grep_search": ToolSecurityDescriptor(effect=ToolEffect.READ, target_argument="path", default_target="."),
    "lsp_navigation": ToolSecurityDescriptor(effect=ToolEffect.READ, target_argument="file"),
    "lsp_diagnostics": ToolSecurityDescriptor(effect=ToolEffect.READ, target_argument="file"),
    "write": ToolSecurityDescriptor(effect=ToolEffect.WRITE, target_argument="path"),
    "edit": ToolSecurityDescriptor(effect=ToolEffect.WRITE, target_argument="path"),
    "aci_edit": ToolSecurityDescriptor(effect=ToolEffect.WRITE, target_argument="path"),
    "ast_grep_replace": ToolSecurityDescriptor(effect=ToolEffect.WRITE, target_argument="path"),
    # Execution.
    "bash": ToolSecurityDescriptor(effect=ToolEffect.EXECUTE, target_argument="command"),
    "bash_input": ToolSecurityDescriptor(effect=ToolEffect.EXECUTION_INPUT, target_argument="line"),
    "process": ToolSecurityDescriptor(effect=ToolEffect.EXECUTION_INPUT, target_argument="data"),
    # Web.
    "web_reader": ToolSecurityDescriptor(effect=ToolEffect.WEB, target_argument="url"),
}

_UNCLASSIFIED = ToolSecurityDescriptor(effect=ToolEffect.NONE)


def describe_tool_security(tool_name: str) -> ToolSecurityDescriptor:
    """The security descriptor for one tool name.

    Unknown names (MCP servers, custom tools) get the NONE default: no
    path claim, existing approval/tool policy still applies.
    """
    return _DESCRIPTORS.get(tool_name, _UNCLASSIFIED)


def extract_call_target(
    descriptor: ToolSecurityDescriptor, arguments: Mapping[str, object]
) -> ToolCallTarget:
    """Extract the claimable target from one call's raw arguments.

    The descriptor names the argument that carries the target; a
    missing/empty optional path takes its declared default. Required paths
    and malformed non-string values make no claim. Unclassified effects
    carry no target at all.
    """
    argument = descriptor.target_argument
    raw = arguments.get(argument) if argument is not None else None
    value = raw if isinstance(raw, str) and raw else None
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        value = descriptor.default_target
    match descriptor.effect:
        case ToolEffect.READ | ToolEffect.WRITE:
            return ToolCallTarget(path=value)
        case ToolEffect.EXECUTE | ToolEffect.EXECUTION_INPUT:
            return ToolCallTarget(command=value)
        case ToolEffect.WEB:
            return ToolCallTarget(url=value)
        case ToolEffect.NONE:
            return ToolCallTarget()
        case unreachable:
            assert_never(unreachable)


def approval_anchor(
    tool_name: str,
    arguments: dict[str, object] | None,
    workspace_root: Path | None,
) -> str | None:
    """The stable identity of one tool call for human-approval anchoring.

    The approval marker binds a ``tool_call_id`` to WHAT the user approved
    on the card. The card renders raw arguments, so the anchor is derived
    from them with the SAME resolution the file tools use: the path
    canonicalized against ``workspace_root`` at the moment the decision is
    applied (the ``WorkspaceScopedFileTool`` rewrite shape — canonical
    exact target, so ``a/../b.txt`` and ``b.txt`` anchor identically and a
    symlinked target anchors to its resolved location).

    ``None`` when the arguments carry nothing anchorable (no path, no
    command, no url, or no declared default) — the call is then not markable and
    the marker check treats it as unmatched. Canonical resolution may touch
    the filesystem. The write site (approval-resume) and read site (backstop)
    MUST pass the same root source for the comparison to be meaningful.
    """
    args = arguments or {}
    descriptor = describe_tool_security(tool_name)
    target = extract_call_target(descriptor, args)
    match descriptor.effect:
        case ToolEffect.READ | ToolEffect.WRITE:
            if target.path is None:
                return None
            try:
                return str(canonicalize_path(target.path, base=workspace_root))
            except PathCanonicalizationError:
                return None
        case ToolEffect.EXECUTE | ToolEffect.EXECUTION_INPUT:
            if descriptor.effect is ToolEffect.EXECUTE and args.get("working_dir") is not None:
                working_dir = args["working_dir"]
                if not isinstance(working_dir, str) or not target.command:
                    return None
                try:
                    path = str(canonicalize_path(working_dir, base=workspace_root))
                except PathCanonicalizationError:
                    return None
                return ToolCallTarget(path=path, command=target.command).model_dump_json(exclude_none=True)
            return target.command or None
        case ToolEffect.WEB:
            return target.url or None
        case ToolEffect.NONE:
            return None
        case unreachable:
            assert_never(unreachable)
