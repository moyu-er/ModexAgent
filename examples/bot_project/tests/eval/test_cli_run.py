from pathlib import Path
from types import SimpleNamespace

import pytest
from bot.eval import cli as eval_cli
from bot.eval.task_spec import EvalToolset
from typer.testing import CliRunner

from modex_agent.ioc.configs.llm import LLMConfig


def _invoke_run(
    monkeypatch: pytest.MonkeyPatch,
    extra_args: list[str],
) -> dict[str, object]:
    captured: dict[str, object] = {}
    provider = SimpleNamespace()

    def build_provider(config: LLMConfig) -> SimpleNamespace:
        captured["provider_model"] = config.model
        captured["provider_api_key"] = config.api_key
        captured["provider_base_url"] = config.base_url
        return provider

    class FakeRunner:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

        def run(self, **kwargs: object) -> SimpleNamespace:
            captured["run"] = kwargs
            return SimpleNamespace(format=lambda: "experiment complete")

    monkeypatch.setattr(
        eval_cli,
        "_load_langfuse_env",
        lambda: ("http://localhost:3000", "public", "secret"),
    )
    monkeypatch.setattr(eval_cli, "Langfuse", lambda **_kwargs: SimpleNamespace())
    monkeypatch.setattr(eval_cli, "EvalRunner", FakeRunner)
    monkeypatch.setattr(eval_cli, "create_llm_provider", build_provider)

    result = CliRunner().invoke(
        eval_cli.app,
        [
            "run",
            "--dataset",
            "w1-smoke",
            "--experiment",
            "discrimination",
            "--model",
            "openai/test-model",
            *extra_args,
        ],
    )

    assert result.exit_code == 0, result.output
    assert captured["provider"] is provider
    assert captured["provider_model"] == "openai/test-model"
    assert captured["model"] == "openai/test-model"
    return captured


def test_run_command_passes_explicit_toolset_and_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _invoke_run(
        monkeypatch,
        ["--toolset", "none", "--mode", "production"],
    )

    assert captured["toolset"] is EvalToolset.NONE
    assert captured["mode"] == "production"
    assert captured["archive_root"] == Path("evals/runs")


def test_run_command_defaults_to_spec_toolset_and_clean_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _invoke_run(monkeypatch, [])

    assert captured["toolset"] is None
    assert captured["mode"] == "clean"
    assert captured["archive_root"] == Path("evals/runs")


def test_run_command_passes_test_llm_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: the credential variables documented by the calibration runbook.
    monkeypatch.setenv("TEST_LLM_API_KEY", "test-key")
    monkeypatch.setenv("TEST_LLM_BASE_URL", "https://llm.example.test/v1")

    # When: the run command constructs its LLM provider.
    captured = _invoke_run(monkeypatch, [])

    # Then: the explicit test credentials reach the provider unchanged.
    assert captured["provider_api_key"] == "test-key"
    assert captured["provider_base_url"] == "https://llm.example.test/v1"


def test_run_command_leaves_test_llm_credentials_optional(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: no test-specific credentials override provider-standard variables.
    monkeypatch.delenv("TEST_LLM_API_KEY", raising=False)
    monkeypatch.delenv("TEST_LLM_BASE_URL", raising=False)

    # When: the run command constructs its LLM provider.
    captured = _invoke_run(monkeypatch, [])

    # Then: empty config values delegate credential resolution to the
    # provider's OPENAI_API_KEY environment fallback.
    assert captured["provider_api_key"] == ""
    assert captured["provider_base_url"] == ""
