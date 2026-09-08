from __future__ import annotations

import pytest
from pydantic import ValidationError

from modex_agent.sandbox.types import EnforcementLevel, SandboxArtifact, SandboxResult


class TestEnforcementLevel:
    def test_values(self) -> None:
        assert EnforcementLevel.FULL.value == "full"
        assert EnforcementLevel.PARTIAL.value == "partial"
        assert EnforcementLevel.NONE.value == "none"

    def test_member_count(self) -> None:
        assert len(EnforcementLevel) == 3


class TestSandboxResultDefaults:
    def test_minimal_construction(self) -> None:
        result = SandboxResult(success=True)
        assert result.success is True
        assert result.stdout == ""
        assert result.stderr == ""
        assert result.artifacts == []
        assert result.execution_time_ms == 0.0
        assert result.error is None

    def test_exit_code_defaults_to_none(self) -> None:
        """exit_code is int | None — 'no process ran' is None, not a lying 0."""
        assert SandboxResult(success=False, error="blocked").exit_code is None

    def test_enforcement_defaults_to_none(self) -> None:
        """Backward compatible: existing call sites report NONE (= current
        safety level) until runtimes populate it."""
        assert SandboxResult(success=True).enforcement is EnforcementLevel.NONE

    def test_exit_code_accepts_int(self) -> None:
        assert SandboxResult(success=False, exit_code=127).exit_code == 127

    def test_enforcement_accepts_level(self) -> None:
        result = SandboxResult(success=True, enforcement=EnforcementLevel.FULL)
        assert result.enforcement is EnforcementLevel.FULL


class TestSandboxResultPydantic:
    def test_frozen(self) -> None:
        result = SandboxResult(success=True)
        with pytest.raises(ValidationError):
            result.success = False

    def test_extra_forbidden(self) -> None:
        with pytest.raises(ValidationError):
            SandboxResult(success=True, no_such_field=1)

    def test_artifact_nesting(self) -> None:
        artifact = SandboxArtifact(path="out.txt", size=3, mime_type="text/plain")
        result = SandboxResult(success=True, artifacts=[artifact])
        assert result.artifacts == [artifact]


class TestSandboxArtifactPydantic:
    def test_frozen(self) -> None:
        artifact = SandboxArtifact(path="a", size=1, mime_type="text/plain")
        with pytest.raises(ValidationError):
            artifact.path = "b"
