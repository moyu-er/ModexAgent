from __future__ import annotations

import io
from unittest.mock import AsyncMock, MagicMock

import pytest
import typer
from bot.eval import cli as eval_cli
from typer.testing import CliRunner


def test_curate_accepts_negative_error_filter_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: a fully mocked curator boundary with no network resources.
    curator = MagicMock()
    curator.curate = AsyncMock(return_value=5)
    monkeypatch.setattr(eval_cli, "DatasetCurator", MagicMock(return_value=curator))
    monkeypatch.setattr(
        eval_cli,
        "_load_langfuse_env",
        lambda: ("http://langfuse.test", "public", "secret"),
    )

    # When: healthy-trace curation disables the default error-only filter.
    result = CliRunner().invoke(
        eval_cli.app,
        ["curate", "--dataset", "smoke", "--max", "5", "--no-filter-errors"],
    )

    # Then: parsing succeeds and the disabled filter reaches DatasetCurator.
    assert result.exit_code == 0
    assert curator.curate.await_args.kwargs["filter_errors"] is False


def test_curate_preserves_existing_filter_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: a fully mocked curator boundary with no network resources.
    curator = MagicMock()
    curator.curate = AsyncMock(return_value=0)
    monkeypatch.setattr(eval_cli, "DatasetCurator", MagicMock(return_value=curator))
    monkeypatch.setattr(
        eval_cli,
        "_load_langfuse_env",
        lambda: ("http://langfuse.test", "public", "secret"),
    )

    # When: curate is invoked without either filter toggle.
    result = CliRunner().invoke(eval_cli.app, ["curate", "--dataset", "default"])

    # Then: error filtering remains enabled and latency filtering remains disabled.
    assert result.exit_code == 0
    assert curator.curate.await_args.kwargs["filter_errors"] is True
    assert curator.curate.await_args.kwargs["filter_high_latency"] is False


def test_console_guard_allows_typer_emoji_output_on_gbk() -> None:
    # Given: the strict GBK text stream used by a default Windows console.
    raw_output = io.BytesIO()
    console = io.TextIOWrapper(raw_output, encoding="gbk")

    # When: CLI startup guards the stream before Typer prints an emoji summary.
    eval_cli._configure_console_stream(console)
    typer.echo("summary 💡", file=console)
    console.flush()

    # Then: the unsupported character is replaced instead of raising.
    assert console.errors == "replace"
    assert "summary ?" in raw_output.getvalue().decode("gbk")
