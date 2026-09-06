"""Read-only command classification — the shell-world parallel fast path.

bash-class tools have no path argument, so their boundary is the command
envelope. But a command that provably performs NO filesystem write and NO
network I/O is the shell-world twin of the parallel (read-only) tool
class: unrestricted like parallel reads — no envelope check, no approval
card. This module is that judge.

Contract (fail-closed everywhere):

- :func:`classify_readonly` returns True only when EVERY simple command
  in the string — every pipeline segment, every ``&&``/``;`` list
  element — resolves to a read-only allowlist entry of the executing
  shell family, with no redirection, no command/process substitution,
  no assignment, and no unknown construct anywhere. Parse failure, an
  unprofiled family, or any doubt returns False and the call takes the
  ordinary exclusive path (envelope boundary / approval).

- This is an approval-experience fast path, NOT a security boundary.
  The kernel substrate (bwrap / Seatbelt / OCI) owns containment; under
  HOST the residual risk is a misclassification executing a writing
  command without approval. ``guard.read_only_bypass`` switches the
  whole layer off if a deployment wants the old friction back.

- Dynamic expansions: a parameter expansion (``$VAR``) is allowed in
  ARGUMENT position of plain read-only commands — the entries have no
  write-capable options, so word-splitting injection can add args but
  not writes. Command-substitution/process-substitution shapes and
  dynamic COMMAND names always fail. Commands with per-argument
  validators (``find``, ``git``) reject any dynamic argument outright:
  their options can change what the command DOES (``-delete``,
  subcommand choice), so a split variable could smuggle a flag.

Shell family: :func:`resolve_command_family` mirrors the executor's own
ladder with the same primitives — kernel substrates always spawn a
bash-family shell (``platform.resolve_shell``); host mode follows the
terminal manager's ``detect_platform_shell`` with the SubprocessTool
fallback (``platform.get_shell_command_args``: CMD on Windows, bash
elsewhere). Judge and executor can therefore never diverge on "what
shell will run this string".
"""

from __future__ import annotations

from typing import TYPE_CHECKING, assert_never

from modex_agent.sandbox.platform import Platform, get_platform
from modex_agent.sandbox.settings import SandboxBackend
from modex_agent.tools.terminal.types import ShellFamily, detect_platform_shell

try:
    import bashlex
except ImportError:  # pragma: no cover — hard dependency; classification fails closed
    bashlex = None  # type: ignore[assignment]

if TYPE_CHECKING:
    from bashlex.ast import node as bash_node

__all__ = ["classify_readonly", "resolve_command_family"]

# -- POSIX (bash/zsh/sh, incl. git-bash) profile ----------------------------

# Display/read-only utilities with NO write-capable option (verified per
# entry: none accepts an output file, and none mutates state).
_BASH_READONLY: frozenset[str] = frozenset({
    "cat", "diff", "df", "du", "echo", "egrep", "fgrep", "file", "grep",
    "head", "ls", "pwd", "rg", "stat", "tail", "type", "wc", "which",
})

# find is read-only unless it carries a filesystem-mutating action flag.
_FIND_WRITE_FLAGS: frozenset[str] = frozenset({
    "-delete", "-exec", "-execdir", "-fls", "-fprint", "-fprintf", "-ok",
    "-okdir",
})

# git subcommands that cannot mutate repository or worktree state.
_GIT_READONLY_SUBCOMMANDS: frozenset[str] = frozenset({
    "blame", "describe", "diff", "grep", "log", "ls-files", "ls-remote",
    "rev-parse", "show", "status",
})

# Expansion child kinds inside a word node.
_SUBSTITUTION_KINDS = frozenset({"commandsubstitution", "processsubstitution"})


def _word_is_literal(word_node: bash_node) -> bool:
    """A word with no expansion children (bashlex marks them via parts)."""
    return not word_node.parts


def _word_has_substitution(word_node: bash_node) -> bool:
    """A word embedding command/process substitution — always a fail."""
    return any(child.kind in _SUBSTITUTION_KINDS for child in word_node.parts)


def _bash_command_readonly(node: bash_node) -> bool:
    """One simple command: allowlisted name, no redirects/assignments."""
    name: str | None = None
    args: list[str] = []
    dynamic_args = False
    for part in node.parts:
        kind = part.kind
        if kind == "redirect" or kind == "assignment":
            return False
        if kind != "word":
            return False
        if name is None:
            # The command name must be literal — a dynamic name can be
            # anything and is the classic bypass shape.
            if not _word_is_literal(part):
                return False
            name = part.word
            continue
        if _word_is_literal(part):
            args.append(part.word)
        elif _word_has_substitution(part):
            return False  # $(...) / <(...) run arbitrary commands
        else:
            dynamic_args = True  # $VAR / ~ — plain expansion in arg position
    if name is None:
        return False
    if name in _BASH_READONLY:
        return True
    if name == "find":
        # A split variable could smuggle an action flag — no dynamic args.
        return not dynamic_args and bool(args) and all(
            arg not in _FIND_WRITE_FLAGS for arg in args
        )
    if name == "git":
        if dynamic_args:
            return False
        subcommand = next((arg for arg in args if not arg.startswith("-")), None)
        return subcommand in _GIT_READONLY_SUBCOMMANDS
    return False


def _bash_node_readonly(node: bash_node) -> bool:
    kind = node.kind
    if kind in ("list", "pipeline"):
        return all(
            _bash_node_readonly(part)
            for part in node.parts
            if part.kind not in ("operator", "pipe")
        )
    if kind == "command":
        return _bash_command_readonly(node)
    # compound / reservedword / function / anything else: fail closed.
    return False


def _bash_readonly(command: str) -> bool:
    if bashlex is None:
        return False
    try:
        trees = bashlex.parse(command)
    except Exception:
        # Malformed or fragmentary input (bash_input continuations) is a
        # normal occurrence — it is simply not provably read-only.
        return False
    return bool(trees) and all(_bash_node_readonly(tree) for tree in trees)


# -- CMD (Windows SubprocessTool fallback) profile ---------------------------

# cmd.exe has no public AST parser, so the profile is deliberately
# narrower: plain segments, no metacharacters beyond separators/pipe.
# `%` (expansion), `^` (escape), `!` (delayed expansion), `<>` (redirect)
# and `()` (blocks) fail closed; quoted arguments are honored only for
# tokenization of the separators.
_CMD_READONLY: frozenset[str] = frozenset({
    "cls", "dir", "echo", "find", "findstr", "hostname", "more", "tasklist",
    "tree", "type", "ver", "vol", "where", "whoami",
})

_CMD_DISQUALIFIED_CHARS = frozenset("%^<>!()")


def _cmd_segments(command: str) -> list[str]:
    """Split on unquoted ``&`` / ``|`` (double quotes only, as cmd does)."""
    segments: list[str] = []
    current: list[str] = []
    in_quote = False
    for ch in command:
        if ch == '"':
            in_quote = not in_quote
            current.append(ch)
        elif ch in "&|" and not in_quote:
            segments.append("".join(current))
            current = []
        else:
            current.append(ch)
    segments.append("".join(current))
    return segments


def _cmd_segment_readonly(segment: str) -> bool:
    stripped = segment.strip()
    if not stripped:
        return True  # empty piece of an `&&`/`||` split carries no command
    if stripped.startswith("@"):  # @echo-style suppression prefix
        stripped = stripped[1:].strip()
    if any(ch in stripped for ch in _CMD_DISQUALIFIED_CHARS):
        return False
    first_token = stripped.split(maxsplit=1)[0].strip('"').lower()
    if first_token.endswith(".exe"):
        first_token = first_token[:-4]
    return first_token in _CMD_READONLY


def _cmd_readonly(command: str) -> bool:
    if "\n" in command or "\r" in command:
        return False
    return all(_cmd_segment_readonly(seg) for seg in _cmd_segments(command))


# -- family resolution + dispatch --------------------------------------------


def resolve_command_family(backend: SandboxBackend) -> ShellFamily:
    """The shell family that will actually interpret command strings.

    Mirrors the executor's ladder with the same primitives: kernel
    substrates (LOCAL/OCI) always spawn a bash-family shell; host mode
    follows ``detect_platform_shell`` and degrades to the SubprocessTool
    fallback (CMD on Windows, bash elsewhere) when no shell is detected.
    """
    if backend in (SandboxBackend.LOCAL, SandboxBackend.OCI):
        return ShellFamily.BASH
    info = detect_platform_shell()
    if info is not None:
        return info.family
    return ShellFamily.CMD if get_platform() is Platform.WINDOWS else ShellFamily.BASH


def classify_readonly(command: str, family: ShellFamily) -> bool:
    """Whether the command provably only reads (no write, no network I/O).

    Fail-closed: unknown or unprofiled families, parse failures, and any
    unrecognized construct return False.
    """
    if not command or not command.strip():
        return False
    match family:
        case ShellFamily.BASH | ShellFamily.ZSH | ShellFamily.SH:
            return _bash_readonly(command)
        case ShellFamily.CMD:
            return _cmd_readonly(command)
        case ShellFamily.POWERSHELL:
            # No sound PowerShell profile yet — no executor routes here.
            return False
        case unreachable:
            assert_never(unreachable)
