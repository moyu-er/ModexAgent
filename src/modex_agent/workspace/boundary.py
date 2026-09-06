"""Canonical path-envelope boundary — the one path seam (ADR-0007 security).

Owns canonicalization (expanduser → anchor to an explicit base →
``resolve(strict=False)``) and multi-root containment (segment-aware
``is_relative_to``, drive/case-aware on Windows, plain segments on POSIX).
Every consumer that must answer "is this path inside the allowed roots?"
converges here: ``WorkspacePolicy``, ``PathBoundaryGuard``,
``ArgumentMatcher``, ``resolve_agent_sandbox``,
``validate_approval_envelope``, ``approval_anchor``.

Design facts:

- Symlinks resolve as far as the filesystem permits; ``strict=False`` retains
  unresolved suffixes rather than requiring targets to exist. Containment
  checks the resolved path, including symlink escapes and links into roots.
  Resolution may access the filesystem; it is not a no-I/O snapshot operation.
- Cross-drive comparisons (``C:\\`` vs ``D:\\``) are a typed denial via
  ``is_relative_to``'s drive-aware semantics — never a raw ``commonpath``
  crash.
- NUL bytes, empty inputs, unresolvable inputs (OS errors from
  expanduser/resolve), and foreign-platform absolute forms (a Windows
  drive/UNC path on POSIX) fail closed with :class:`PathCanonicalizationError`.
- Stdlib-only (workspace/AGENTS.md dependency contract): no pydantic, no
  framework imports.
"""

from __future__ import annotations

import os
from pathlib import Path, PureWindowsPath

__all__ = [
    "PathCanonicalizationError",
    "PathEnvelope",
    "canonicalize_path",
    "contains_path",
    "resolve_against",
]


class PathCanonicalizationError(ValueError):
    """A path input cannot be canonicalized.

    Covers empty/NUL-bearing input, OS-level resolution failures
    (expanduser/resolve), and foreign-platform absolute forms. Fail-closed:
    callers treat this as "unresolvable ⇒ not contained" and surface it —
    never fall back to the raw string.
    """


def _reject_foreign_absolute(text: str) -> None:
    """Fail closed on a foreign-platform absolute form.

    A Windows drive-letter (``C:\\x``) or UNC (``\\\\server\\share``) form is
    absolute in Windows semantics and is rejected on POSIX hosts.
    Re-anchoring it under ``base`` would fabricate an
    in-root subdirectory (``/ws/C:\\Windows``), and resolving it under the
    process cwd is arbitrary — both launder a foreign absolute into the
    envelope, so it is rejected instead.
    """
    if os.name != "nt" and PureWindowsPath(text).is_absolute():
        raise PathCanonicalizationError(
            f"foreign Windows absolute path form rejected: {text!r}"
        )


def canonicalize_path(
    raw: Path | str,
    *,
    base: Path | None = None,
) -> Path:
    """Canonicalize *raw*: expanduser, anchor relatives to *base*, resolve.

    ``base`` anchors relative inputs, normally to the call site's workspace
    root; when omitted, resolution uses the process working directory. Absolute
    inputs ignore it. ``~`` expands to the user home per
    ``Path.expanduser``. Nonempty path spelling, including surrounding
    whitespace, is preserved. Symlink resolution may touch the filesystem.

    Raises:
        PathCanonicalizationError: empty/NUL input, a foreign-platform
            absolute form, or an OS-level resolution failure.
    """
    text = raw if isinstance(raw, str) else str(raw)
    if not text.strip():
        raise PathCanonicalizationError("empty path input cannot be canonicalized")
    if "\x00" in text:
        raise PathCanonicalizationError(
            f"path input carries a NUL byte and cannot address a file: {raw!r}"
        )
    _reject_foreign_absolute(text)
    try:
        candidate = Path(text).expanduser()
        if not candidate.is_absolute():
            anchor = base if base is not None else Path.cwd()
            candidate = anchor / candidate
        return candidate.resolve(strict=False)
    except (ValueError, OSError, RuntimeError) as exc:
        raise PathCanonicalizationError(
            f"path input {raw!r} cannot be canonicalized against "
            f"{str(base)!r}: {exc}"
        ) from exc


class PathEnvelope:
    """The canonical containment envelope: an ordered set of resolved roots.

    A behavior-bearing regular class (project rule 11) — immutable by
    convention, not by freeze machinery: ``__init__`` assigns the
    canonicalized root tuple once and ``roots`` exposes it read-only; no
    consumer mutates it (rule 17 — fix the mutation, don't fence it).
    Roots canonicalize at construction (relative roots anchor to the
    caller-supplied ``base``). Stored roots stay fixed, while each containment
    query canonicalizes its target. Callers rebuild envelopes from live roots
    when required; delegated permission roots are captured at materialization.
    """

    __slots__ = ("_roots",)

    def __init__(
        self,
        roots: tuple[Path | str, ...] | list[Path | str] | None = None,
        *,
        base: Path | None = None,
    ) -> None:
        if roots is None:
            roots = ()
        self._roots = tuple(canonicalize_path(root, base=base) for root in roots)

    @property
    def roots(self) -> tuple[Path, ...]:
        """The canonicalized envelope roots (read-only tuple)."""
        return self._roots

    def __repr__(self) -> str:
        return f"PathEnvelope(roots={self._roots!r})"

    def contains(self, path: Path | str, *, base: Path | None = None) -> bool:
        """True iff *path* canonicalizes inside at least one root.

        Never raises for boundary questions: an uncanonicalizable input is
        simply not contained (fail-closed).
        """
        try:
            resolved = canonicalize_path(path, base=base)
        except PathCanonicalizationError:
            return False
        return any(resolved.is_relative_to(root) for root in self._roots)


def contains_path(
    envelope: PathEnvelope,
    path: Path | str,
    *,
    base: Path | None = None,
) -> bool:
    """Free-function containment over *envelope* (see ``PathEnvelope.contains``)."""
    return envelope.contains(path, base=base)


def resolve_against(envelope: PathEnvelope, path: Path | str) -> Path:
    """Canonicalize *path* against the envelope's first root (its base).

    The first root IS the workspace root by construction — the anchor
    relative tool paths use.
    """
    if not envelope.roots:
        raise PathCanonicalizationError("cannot resolve against an empty envelope")
    return canonicalize_path(path, base=envelope.roots[0])
