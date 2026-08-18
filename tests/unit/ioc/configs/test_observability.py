"""Unit tests for ObservabilityConfig loading and defaults."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from pydantic import ValidationError

from modex_agent.ioc.configs.app import AppConfig
from modex_agent.ioc.configs.observability import (
    CassetteScope,
    ObservabilityConfig,
    PromptCaptureMode,
    TraceBackend,
    TraceSpanMode,
)


class TestObservabilityConfigDefaults:
    """Default values produce byte-for-byte today's behavior."""

    def test_existing_defaults_unchanged(self) -> None:
        cfg = ObservabilityConfig()
        assert cfg.run_logging is True
        assert cfg.level == "INFO"

    def test_new_field_defaults(self) -> None:
        cfg = ObservabilityConfig()
        assert cfg.trace_backend is TraceBackend.FILE
        assert cfg.otel_endpoint is None
        assert cfg.otel_service_name == "modex_agent"
        assert cfg.retain_reasoning_content is True
        assert cfg.checkpoint_per_iteration is True
        assert cfg.cassette_enabled is False
        assert cfg.cassette_scope is CassetteScope.DEFAULT
        assert cfg.training_relevant is False
        assert cfg.training_max_iterations == 20
        assert cfg.training_max_tokens == 100000
        assert cfg.prompt_capture is PromptCaptureMode.SUMMARY
        assert cfg.trace_spans is TraceSpanMode.STANDARD
        assert cfg.capture_tools is False
        assert cfg.eval_ingestion_url is None

    def test_eval_ingestion_url_explicit_string(self) -> None:
        cfg = ObservabilityConfig(
            eval_ingestion_url="https://lf.example.invalid/api/public/ingestion"
        )
        assert cfg.eval_ingestion_url == "https://lf.example.invalid/api/public/ingestion"

    def test_prompt_capture_mode_enum_values(self) -> None:
        assert PromptCaptureMode.OFF == "off"
        assert PromptCaptureMode.HASH == "hash"
        assert PromptCaptureMode.SUMMARY == "summary"
        assert PromptCaptureMode.FULL == "full"

    def test_trace_span_mode_enum_values(self) -> None:
        assert TraceSpanMode.MINIMAL == "minimal"
        assert TraceSpanMode.STANDARD == "standard"
        assert TraceSpanMode.FULL == "full"

    def test_string_coercion(self) -> None:
        cfg = ObservabilityConfig(prompt_capture="full", trace_spans="minimal")  # type: ignore[arg-type]
        assert cfg.prompt_capture is PromptCaptureMode.FULL
        assert cfg.trace_spans is TraceSpanMode.MINIMAL

    def test_frozen(self) -> None:
        cfg = ObservabilityConfig()
        with pytest.raises(ValidationError):
            cfg.run_logging = False  # type: ignore[misc]

    def test_extra_forbidden(self) -> None:
        with pytest.raises(ValidationError):
            ObservabilityConfig(unknown_field=True)  # type: ignore[call-arg]


class TestObservabilityConfigFromYaml:
    """Loading a bot_config.yml with an observability section populates fields."""

    def test_full_observability_section(self) -> None:
        yaml_content = """
observability:
  run_logging: false
  level: "DEBUG"
  trace_backend: "otel_http"
  otel_endpoint: "http://localhost:4318/v1/traces"
  otel_service_name: "my-service"
  retain_reasoning_content: false
  checkpoint_per_iteration: false
  cassette_enabled: true
  cassette_scope: "full"
  training_relevant: true
  training_max_iterations: 50
  training_max_tokens: 200000
"""
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yml", delete=False, encoding="utf-8",
        ) as f:
            f.write(yaml_content)
            tmp = f.name

        try:
            cfg = AppConfig.from_yaml(tmp)
            assert cfg.observability is not None
            obs = cfg.observability
            assert obs.run_logging is False
            assert obs.level == "DEBUG"
            assert obs.trace_backend is TraceBackend.OTEL_HTTP
            assert obs.otel_endpoint == "http://localhost:4318/v1/traces"
            assert obs.otel_service_name == "my-service"
            assert obs.retain_reasoning_content is False
            assert obs.checkpoint_per_iteration is False
            assert obs.cassette_enabled is True
            assert obs.cassette_scope is CassetteScope.FULL
            assert obs.training_relevant is True
            assert obs.training_max_iterations == 50
            assert obs.training_max_tokens == 200000
        finally:
            Path(tmp).unlink()

    def test_minimal_observability_section(self) -> None:
        """Only existing fields — new fields get defaults."""
        yaml_content = """
observability:
  run_logging: true
  level: "INFO"
"""
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yml", delete=False, encoding="utf-8",
        ) as f:
            f.write(yaml_content)
            tmp = f.name

        try:
            cfg = AppConfig.from_yaml(tmp)
            assert cfg.observability is not None
            obs = cfg.observability
            assert obs.run_logging is True
            assert obs.level == "INFO"
            assert obs.trace_backend is TraceBackend.FILE
            assert obs.otel_endpoint is None
            assert obs.cassette_enabled is False
            assert obs.training_relevant is False
        finally:
            Path(tmp).unlink()

    def test_no_observability_section(self) -> None:
        """Absent observability section → None (no logging)."""
        yaml_content = """
paths:
  data_dir: "data"
"""
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yml", delete=False, encoding="utf-8",
        ) as f:
            f.write(yaml_content)
            tmp = f.name

        try:
            cfg = AppConfig.from_yaml(tmp)
            assert cfg.observability is None
        finally:
            Path(tmp).unlink()
