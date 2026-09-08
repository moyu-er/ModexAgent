from __future__ import annotations

import pytest
from pydantic import ValidationError

from modex_agent.sandbox.guard import CommandPatternGuard, CommandPatternGuardConfig
from modex_agent.sandbox.guard_network import NetworkGuard
from modex_agent.sandbox.guard_pipeline import GuardPipeline


class TestGuardPipeline:
    """Composite guard pipeline runs multiple guards in sequence."""

    def test_pipeline_allows_safe_command(self) -> None:
        pipeline = GuardPipeline([
            CommandPatternGuard(),
            NetworkGuard(),
        ])
        result = pipeline.check("echo hello")
        assert result.allowed

    def test_pipeline_blocks_pattern_match(self) -> None:
        pipeline = GuardPipeline([
            CommandPatternGuard(),
            NetworkGuard(),
        ])
        result = pipeline.check("rm -rf /")
        assert not result.allowed
        assert any(m.category == "destructive" for m in result.matches)

    def test_pipeline_blocks_ssrf(self) -> None:
        pipeline = GuardPipeline([
            CommandPatternGuard(),
            NetworkGuard(),
        ])
        result = pipeline.check("curl http://127.0.0.1/x")
        assert not result.allowed
        assert any(m.category == "ssrf" for m in result.matches)

    def test_pipeline_short_circuits_on_first_denial(self) -> None:
        """If pattern guard denies, the network guard is not reached."""
        pipeline = GuardPipeline([
            CommandPatternGuard(),
            NetworkGuard(),
        ])
        result = pipeline.check("rm -rf /")
        assert not result.allowed
        # Only pattern matches, no network match
        assert any(m.category == "destructive" for m in result.matches)
        assert not any(m.category == "ssrf" for m in result.matches)

    def test_pipeline_with_allow_patterns(self) -> None:
        config = CommandPatternGuardConfig(allow_patterns=[r"\bsudo\b"])
        pipeline = GuardPipeline([
            CommandPatternGuard(config),
            NetworkGuard(),
        ])
        result = pipeline.check("sudo apt update")
        assert result.allowed

    def test_empty_pipeline_allows_all(self) -> None:
        pipeline = GuardPipeline([])
        result = pipeline.check("rm -rf /")
        assert result.allowed

    def test_pipeline_combined_denial_reason(self) -> None:
        """When both guards would deny, only first denial is reported (short-circuit)."""
        pipeline = GuardPipeline([
            CommandPatternGuard(),
            NetworkGuard(),
        ])
        result = pipeline.check("rm -rf /")
        assert not result.allowed
        assert "destructive" in result.reason


class TestGuardPipelinePydantic:
    """GuardPipeline is a frozen pydantic model (rule 12) — not @dataclass."""

    def test_positional_guard_list_construction(self) -> None:
        pipeline = GuardPipeline([CommandPatternGuard()])
        assert len(pipeline.guards) == 1

    def test_empty_default(self) -> None:
        assert GuardPipeline().guards == []

    def test_frozen(self) -> None:
        pipeline = GuardPipeline([CommandPatternGuard()])
        with pytest.raises(ValidationError):
            pipeline.guards = []
