from __future__ import annotations

import os
import tempfile

import pytest

from framework.sandbox.exceptions import WorkspaceBoundaryError
from framework.sandbox.workspace_policy import WorkspacePolicy, WorkspacePolicyConfig


class TestWorkspacePolicyResolvePath:
    """resolve_path correctly resolves and validates paths."""

    @pytest.fixture
    def policy(self, tmp_path) -> WorkspacePolicy:
        return WorkspacePolicy(WorkspacePolicyConfig(root=str(tmp_path)))

    def test_absolute_path_within(self, policy: WorkspacePolicy, tmp_path) -> None:
        resolved = policy.resolve_path(str(tmp_path / "file.txt"))
        assert resolved == (tmp_path / "file.txt").resolve()

    def test_relative_path_resolved(self, policy: WorkspacePolicy, tmp_path) -> None:
        resolved = policy.resolve_path("file.txt")
        assert resolved == (tmp_path / "file.txt").resolve()

    def test_traversal_blocked(self, policy: WorkspacePolicy) -> None:
        with pytest.raises(WorkspaceBoundaryError):
            policy.resolve_path("../../../etc/passwd")

    def test_absolute_path_outside_blocked(self, policy: WorkspacePolicy) -> None:
        with pytest.raises(WorkspaceBoundaryError):
            policy.resolve_path("/etc/passwd")


class TestWorkspacePolicyIsWithin:
    """is_within returns bool without raising."""

    @pytest.fixture
    def policy(self, tmp_path) -> WorkspacePolicy:
        return WorkspacePolicy(WorkspacePolicyConfig(root=str(tmp_path)))

    def test_path_within_returns_true(self, policy: WorkspacePolicy, tmp_path) -> None:
        assert policy.is_within(str(tmp_path / "subdir" / "file.txt")) is True

    def test_path_outside_returns_false(self, policy: WorkspacePolicy) -> None:
        assert policy.is_within("/etc/passwd") is False

    def test_relative_within(self, policy: WorkspacePolicy) -> None:
        assert policy.is_within("file.txt") is True


class TestWorkspacePolicyRequireWithin:
    """require_within raises WorkspaceBoundaryError for paths outside root."""

    @pytest.fixture
    def policy(self, tmp_path) -> WorkspacePolicy:
        return WorkspacePolicy(WorkspacePolicyConfig(root=str(tmp_path)))

    def test_within_does_not_raise(self, policy: WorkspacePolicy, tmp_path) -> None:
        policy.require_within(str(tmp_path / "file.txt"))

    def test_outside_raises(self, policy: WorkspacePolicy) -> None:
        with pytest.raises(WorkspaceBoundaryError):
            policy.require_within("/etc/passwd")


class TestWorkspacePolicyEnforceFalse:
    """enforce=False disables all boundary checks."""

    @pytest.fixture
    def policy(self, tmp_path) -> WorkspacePolicy:
        return WorkspacePolicy(WorkspacePolicyConfig(root=str(tmp_path), enforce=False))

    def test_resolve_path_allows_escape(self, policy: WorkspacePolicy) -> None:
        # Should not raise even though path is outside
        resolved = policy.resolve_path("/etc/passwd")
        assert resolved is not None

    def test_is_within_returns_true(self, policy: WorkspacePolicy) -> None:
        assert policy.is_within("/etc/passwd") is True


class TestWorkspacePolicyAllowPaths:
    """Extra allowed paths extend the boundary."""

    @pytest.fixture
    def policy(self, tmp_path) -> WorkspacePolicy:
        return WorkspacePolicy(WorkspacePolicyConfig(
            root=str(tmp_path),
            allow_paths=(str(tmp_path / "external"),),
        ))

    def test_allowed_path_passes(self, policy: WorkspacePolicy, tmp_path) -> None:
        assert policy.is_within(str(tmp_path / "external" / "file.txt")) is True

    def test_unlisted_path_fails(self, policy: WorkspacePolicy) -> None:
        assert policy.is_within("/opt/other") is False


class TestWorkspacePolicyRoot:
    """root property returns resolved path."""

    def test_root_is_resolved(self, tmp_path) -> None:
        policy = WorkspacePolicy(WorkspacePolicyConfig(root=str(tmp_path)))
        assert policy.root == tmp_path.resolve()
