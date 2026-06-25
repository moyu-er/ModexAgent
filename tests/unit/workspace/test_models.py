from __future__ import annotations

from pathlib import Path

import pytest

from modex_agent.workspace.models import CdResult


class TestCdResult:
    def test_success_result(self):
        result = CdResult(
            success=True,
            current_path=Path("/foo"),
            original_path=Path("/home"),
            notice="switched to: /foo",
        )
        assert result.success is True
        assert result.current_path == Path("/foo")
        assert result.original_path == Path("/home")
        assert result.error is None

    def test_failure_result(self):
        result = CdResult(
            success=False,
            current_path=Path("/home"),
            original_path=Path("/home"),
            notice="cd: path not found: '/nonexist'",
            error="path_not_found",
        )
        assert result.success is False
        assert result.current_path == Path("/home")
        assert result.error == "path_not_found"

    def test_notice_contains_current_path(self):
        result = CdResult(
            success=True,
            current_path=Path("/tmp"),
            original_path=Path("/home"),
            notice="switched to: /tmp",
        )
        assert "/tmp" in result.notice

    def test_frozen_dataclass_cannot_be_modified(self):
        result = CdResult(
            success=True,
            current_path=Path("/foo"),
            original_path=Path("/home"),
            notice="test",
        )
        with pytest.raises(AttributeError):
            result.success = False
