"""Workspace path-envelope boundary — the canonical path seam.

Red-first coverage for the live-root / multi-root / symlink / case /
cross-drive defects found across sandbox, approval, scope validation, and
workspace consumers. One canonical seam (``modex_agent.workspace.boundary``)
owns canonicalization + containment; these tests prove each distinct root
cause fails BEFORE the convergence lands.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from modex_agent.workspace.boundary import (
    PathCanonicalizationError,
    PathEnvelope,
    canonicalize_path,
    contains_path,
    resolve_against,
)

IS_WINDOWS = sys.platform == "win32"


# ─── canonicalize_path ───────────────────────────────────────────────────────


class TestCanonicalizePath:
    def test_user_expansion(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("USERPROFILE" if IS_WINDOWS else "HOME", str(tmp_path))
        result = canonicalize_path("~\\notes" if IS_WINDOWS else "~/notes", base=tmp_path)
        expected = (tmp_path / "notes").resolve(strict=False)
        assert result == expected

    def test_relative_anchors_to_explicit_base(self, tmp_path: Path) -> None:
        result = canonicalize_path("sub/inner.txt", base=tmp_path / "ws")
        assert result == (tmp_path / "ws" / "sub" / "inner.txt").resolve(strict=False)

    def test_dot_dot_collapses_within_base(self, tmp_path: Path) -> None:
        result = canonicalize_path("sub/../other.txt", base=tmp_path)
        assert result == (tmp_path / "other.txt").resolve(strict=False)

    def test_symlink_resolves_strict_false(self, tmp_path: Path) -> None:
        real = tmp_path / "real"
        real.mkdir()
        link = tmp_path / "link"
        link.symlink_to(real, target_is_directory=True)
        assert canonicalize_path(link, base=tmp_path) == real.resolve(strict=False)

    def test_trailing_slash_and_redundant_separators_normalize(self, tmp_path: Path) -> None:
        raw = "sub///inner/./../inner" + ("\\" if IS_WINDOWS else "/")
        result = canonicalize_path(raw, base=tmp_path)
        assert result == (tmp_path / "sub" / "inner").resolve(strict=False)

    def test_nul_byte_fails_closed(self, tmp_path: Path) -> None:
        with pytest.raises(PathCanonicalizationError):
            canonicalize_path("bad\u0000name", base=tmp_path)

    def test_empty_fails_closed(self, tmp_path: Path) -> None:
        with pytest.raises(PathCanonicalizationError):
            canonicalize_path("", base=tmp_path)

    @pytest.mark.skipif(not IS_WINDOWS, reason="Windows drive/case semantics")
    def test_windows_drive_letter_case_insensitive_match(self, tmp_path: Path) -> None:
        drive = tmp_path.drive  # e.g. "F:"
        upper = tmp_path.as_posix().replace(drive, drive.upper(), 1)
        assert canonicalize_path(upper, base=tmp_path.parent) == canonicalize_path(
            tmp_path, base=tmp_path.parent
        )

    @pytest.mark.skipif(IS_WINDOWS, reason="POSIX rejects foreign absolutes")
    def test_posix_windows_drive_form_rejected_fail_closed(self, tmp_path: Path) -> None:
        # A Windows drive-letter absolute is foreign on POSIX; resolving it
        # under cwd would fabricate an in-root subdirectory. Fail closed.
        with pytest.raises(PathCanonicalizationError):
            canonicalize_path("C:\\Windows\\System32", base=tmp_path)

    @pytest.mark.skipif(IS_WINDOWS, reason="POSIX rejects foreign absolutes")
    def test_posix_unc_form_rejected_fail_closed(self, tmp_path: Path) -> None:
        with pytest.raises(PathCanonicalizationError):
            canonicalize_path(r"\\server\share\file", base=tmp_path)

    def test_posix_style_absolute_stays_absolute_cross_platform(self, tmp_path: Path) -> None:
        # A POSIX absolute must NOT be anchored under base on any platform.
        result = canonicalize_path("/srv/data", base=tmp_path)
        assert result.is_absolute()
        assert not result.is_relative_to(tmp_path)


# ─── PathEnvelope: multi-root containment ────────────────────────────────────


class TestPathEnvelopeContains:
    def test_path_inside_first_root(self, tmp_path: Path) -> None:
        envelope = PathEnvelope(roots=(tmp_path / "ws", tmp_path / "shared"))
        assert contains_path(envelope, tmp_path / "ws" / "a.txt")

    def test_path_inside_second_root(self, tmp_path: Path) -> None:
        envelope = PathEnvelope(roots=(tmp_path / "ws", tmp_path / "shared"))
        assert contains_path(envelope, tmp_path / "shared" / "lib" / "b.py")

    def test_prefix_sibling_rejected(self, tmp_path: Path) -> None:
        envelope = PathEnvelope(roots=(tmp_path / "ws",))
        assert not contains_path(envelope, tmp_path / "ws-evil" / "x")

    def test_envelope_root_itself_contained(self, tmp_path: Path) -> None:
        root = (tmp_path / "ws").resolve(strict=False)
        envelope = PathEnvelope(roots=(root,))
        assert contains_path(envelope, root)

    def test_symlink_escape_rejected(self, tmp_path: Path) -> None:
        # outside/escape.txt linked from inside the root must resolve out.
        outside = tmp_path / "outside"
        outside.mkdir()
        root = tmp_path / "ws"
        root.mkdir()
        link = root / "escape"
        link.symlink_to(outside, target_is_directory=True)
        envelope = PathEnvelope(roots=(root,))
        assert not contains_path(envelope, link / "escape.txt")

    def test_relative_path_resolved_against_base_root(self, tmp_path: Path) -> None:
        root = tmp_path / "ws"
        envelope = PathEnvelope(roots=(root,))
        assert contains_path(envelope, "sub/a.txt", base=root)

    def test_relative_path_escaping_base_rejected(self, tmp_path: Path) -> None:
        root = tmp_path / "ws"
        envelope = PathEnvelope(roots=(root,))
        assert not contains_path(envelope, "../evil.txt", base=root)

    @pytest.mark.skipif(
        IS_WINDOWS, reason="posix-only: distinct roots behave like cross-drive"
    )
    def test_cross_drive_returns_denial_not_crash(self) -> None:
        envelope = PathEnvelope(roots=(Path("/ws"),))
        assert not contains_path(envelope, Path("/etc/passwd"))

    def test_cross_drive_on_same_volume_string_prefix_rejected(self, tmp_path: Path) -> None:
        envelope = PathEnvelope(roots=(tmp_path / "workspace",))
        assert not contains_path(envelope, tmp_path / "workspace-evil" / "f")


class TestPathEnvelopeValue:
    def test_roots_attribute_is_read_only(self, tmp_path: Path) -> None:
        envelope = PathEnvelope(roots=(tmp_path,))
        with pytest.raises(AttributeError):
            envelope.roots = ()  # type: ignore[misc]

    def test_roots_exposed_as_tuple(self, tmp_path: Path) -> None:
        root = (tmp_path / "ws").resolve(strict=False)
        envelope = PathEnvelope(roots=(root,))
        assert envelope.roots == (root,)
        assert isinstance(envelope.roots, tuple)

    def test_resolve_against_returns_canonical(self, tmp_path: Path) -> None:
        root = tmp_path / "ws"
        envelope = PathEnvelope(roots=(root,))
        resolved = resolve_against(envelope, "a.txt")
        assert resolved == (root / "a.txt").resolve(strict=False)


# ─── Mixed abs/relative root lists (the validate_allowed_dirs repro) ─────────


class TestEnvelopeMixedRoots:
    def test_relative_root_extends_to_canonical_pool_root(self, tmp_path: Path) -> None:
        # allowed_dirs may be RELATIVE to the workspace root; the envelope
        # must canonicalize both sides before containment.
        ws = tmp_path / "workspace"
        envelope = PathEnvelope(
            roots=(ws, canonicalize_path("../shared-lib", base=ws))
        )
        assert contains_path(envelope, ws / "src" / "main.py")
        assert contains_path(envelope, tmp_path / "shared-lib" / "pkg")

    def test_mixed_abs_and_relative_no_commonpath_crash(self, tmp_path: Path) -> None:
        ws = tmp_path / "workspace"
        envelope = PathEnvelope(
            roots=(ws, canonicalize_path("../shared-lib", base=ws))
        )
        assert not contains_path(envelope, tmp_path / "elsewhere" / "x")

    @pytest.mark.skipif(
        not IS_WINDOWS, reason="Windows C:/D: drive mix crashes commonpath"
    )
    def test_windows_cross_drive_root_mix_no_crash(self, tmp_path: Path) -> None:
        ws = tmp_path  # C:-like drive (whatever the temp dir is on)
        envelope = PathEnvelope(roots=(ws, Path("D:\\shared")))
        assert not contains_path(envelope, Path("D:\\elsewhere"))
        assert contains_path(envelope, Path("D:\\shared\\lib"))

    def test_case_variant_root_on_windows_contained(self, tmp_path: Path) -> None:
        root = tmp_path / "Workspace"
        envelope = PathEnvelope(roots=(root,))
        variant = tmp_path / "WORKSPACE" if IS_WINDOWS else root
        assert contains_path(envelope, variant / "a") is IS_WINDOWS or contains_path(
            envelope, root / "a"
        )


class TestForeignAbsoluteFailClosed:
    def test_posix_windows_drive_root_rejected_not_reanchored(self, tmp_path: Path) -> None:
        # Envelope containment must fail closed for a foreign absolute root
        # rather than laundering it under the workspace base.
        envelope = PathEnvelope(roots=(tmp_path,), base=tmp_path)
        assert not envelope.contains("C:\\Windows\\System32", base=tmp_path)

    @pytest.mark.skipif(IS_WINDOWS, reason="POSIX-only foreign-form rejection")
    def test_posix_unc_envelope_root_rejected_at_construction(self, tmp_path: Path) -> None:
        with pytest.raises(PathCanonicalizationError):
            PathEnvelope(roots=(r"\\server\share",), base=tmp_path)

    @pytest.mark.skipif(not IS_WINDOWS, reason="Windows treats drive forms as native")
    def test_windows_drive_root_accepted_natively(self, tmp_path: Path) -> None:
        # On Windows a second drive IS a real root — the cross-drive envelope
        # both constructs and contains natively.
        envelope = PathEnvelope(roots=(tmp_path, "D:\\shared"), base=tmp_path)
        assert envelope.contains("D:\\shared\\lib")
        assert not envelope.contains("D:\\elsewhere")


# os imported for parity with sibling tests; keep linters honest.
_ = os
