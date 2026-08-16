from pathlib import Path
from types import SimpleNamespace

import pytest
from bot.eval import cli as eval_cli
from click.testing import Result
from langfuse.api.commons.errors.not_found_error import NotFoundError
from typer.testing import CliRunner


def _invoke_compare_after_endpoint_404(
    monkeypatch: pytest.MonkeyPatch,
    dataset: str,
) -> Result:
    def raise_not_found(**_kwargs: object) -> None:
        raise NotFoundError({"message": "endpoint not available on this deployment"})

    monkeypatch.setattr(
        eval_cli,
        "_load_langfuse_env",
        lambda: ("http://localhost:3000", "public", "secret"),
    )
    monkeypatch.setattr(
        eval_cli,
        "Langfuse",
        lambda **_kwargs: SimpleNamespace(get_dataset_runs=raise_not_found),
    )

    return CliRunner().invoke(eval_cli.app, ["compare", "--dataset", dataset])


def test_compare_degrades_gracefully_on_not_found_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = _invoke_compare_after_endpoint_404(monkeypatch, "no-such-dataset")

    assert result.exit_code == 0, result.output
    assert (
        "Dataset runs unavailable on this Langfuse deployment (HTTP 404)"
        in result.output
    )
    assert "No local run archives found." in result.output


def test_compare_lists_local_run_archives_after_endpoint_404(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    archive_dir = tmp_path / "evals" / "runs" / "w1-smoke" / "discrim-full"
    archive_dir.mkdir(parents=True)
    (archive_dir / "20260815T040733.005886Z.json").write_text("{}", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    result = _invoke_compare_after_endpoint_404(monkeypatch, "w1-smoke")

    assert result.exit_code == 0, result.output
    expected_archive = str(Path("evals") / "runs" / "w1-smoke")
    assert f"local run archives: {expected_archive}" in result.output
    assert "discrim-full: 1 run(s)" in result.output
