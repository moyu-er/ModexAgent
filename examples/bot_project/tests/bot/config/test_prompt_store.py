"""Tests for bot.config.prompt_store (Task 2.5). All tmp_path."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_BOT_PROJECT = Path(__file__).resolve().parents[3]
if str(_BOT_PROJECT) not in sys.path:
    sys.path.insert(0, str(_BOT_PROJECT))

from bot.config import PromptContent  # noqa: E402
from bot.config.prompt_store import (  # noqa: E402
    PromptStore,
    PromptValidationError,
    UnknownPromptError,
)


@pytest.fixture
def store(tmp_path: Path) -> PromptStore:
    return PromptStore(base_dir=tmp_path)


class TestRoundTrip:
    def test_write_then_read(self, store: PromptStore) -> None:
        out = store.write_prompt("scout", "You are a scout.\n")
        assert out == PromptContent(name="scout", content="You are a scout.\n")
        reread = store.read_prompt("scout")
        assert reread.content == "You are a scout.\n"

    def test_preserves_trailing_newline(self, store: PromptStore) -> None:
        store.write_prompt("scout", "no newline")
        # Content is written verbatim — no forced newline.
        assert store.read_prompt("scout").content == "no newline"

    def test_overwrite(self, store: PromptStore) -> None:
        store.write_prompt("scout", "v1")
        store.write_prompt("scout", "v2")
        assert store.read_prompt("scout").content == "v2"

    def test_no_tmp_left_behind(self, store: PromptStore, tmp_path: Path) -> None:
        store.write_prompt("scout", "x")
        assert not (tmp_path / "agents" / "scout.md.tmp").exists()

    def test_utf8_content(self, store: PromptStore) -> None:
        store.write_prompt("scout", "你是侦察兵\n")
        assert store.read_prompt("scout").content == "你是侦察兵\n"


class TestErrors:
    def test_missing_raises(self, store: PromptStore) -> None:
        with pytest.raises(UnknownPromptError):
            store.read_prompt("nope")

    @pytest.mark.parametrize("bad", ["..", "a/b", "a\\b", "A", "1abc", "x y", "-x"])
    def test_bad_agent_name_rejected_on_read(
        self, store: PromptStore, bad: str
    ) -> None:
        with pytest.raises((PromptValidationError, UnknownPromptError)):
            store.read_prompt(bad)

    @pytest.mark.parametrize("bad", ["..", "a/b", "A", "1abc", "-x"])
    def test_bad_agent_name_rejected_on_write(
        self, store: PromptStore, bad: str
    ) -> None:
        with pytest.raises(PromptValidationError):
            store.write_prompt(bad, "x")

    def test_prompt_exists(self, store: PromptStore) -> None:
        assert store.prompt_exists("scout") is False
        store.write_prompt("scout", "x")
        assert store.prompt_exists("scout") is True
