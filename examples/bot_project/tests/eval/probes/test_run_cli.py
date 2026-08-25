from __future__ import annotations

from pathlib import Path

from bot.eval.probes import run_cli
from typer.testing import CliRunner

_BOT_PROJECT_DIR = Path(__file__).resolve().parents[3]
_LIBRARY = _BOT_PROJECT_DIR / "evals" / "probes" / "frozen_v1.jsonl"
_MANIFEST = _BOT_PROJECT_DIR / "evals" / "probes" / "manifest_v1.json"


def test_cli_parses_required_dispatch_arguments_without_network(monkeypatch) -> None:
    # Given
    captured: list[run_cli.ProbeCliOptions] = []

    async def scripted_dispatch(options: run_cli.ProbeCliOptions) -> Path:
        captured.append(options)
        return Path("evals/evidence/b5_first_run.json")

    monkeypatch.setattr(run_cli, "dispatch_probe_run", scripted_dispatch)

    # When
    result = CliRunner().invoke(
        run_cli.app,
        [
            "--library",
            str(_LIBRARY),
            "--manifest",
            str(_MANIFEST),
            "--run-name",
            "memory-probes.smoke-1",
            "--max-cost",
            "1.00",
        ],
    )

    # Then
    assert result.exit_code == 0, result.output
    assert captured == [
        run_cli.ProbeCliOptions(
            library=_LIBRARY,
            manifest=_MANIFEST,
            run_name="memory-probes.smoke-1",
            max_cost_usd=1.0,
        )
    ]
