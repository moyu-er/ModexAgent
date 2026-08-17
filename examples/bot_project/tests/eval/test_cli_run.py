from pathlib import Path
from types import SimpleNamespace

import pytest
from bot.eval import cli as eval_cli
from bot.eval.task_spec import EvalToolset
from typer.testing import CliRunner


def _invoke_run(
    monkeypatch: pytest.MonkeyPatch,
    extra_args: list[str],
) -> dict[str, object]:
    captured: dict[str, object] = {}
    provider = SimpleNamespace()

    def build_provider(*, model: str) -> SimpleNamespace:
        captured["provider_model"] = model
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
    monkeypatch.setattr("modex_agent.providers.LiteLLMProvider", build_provider)

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
