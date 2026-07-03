"""Tests for modexbot.config_model — model.yml read/write helpers."""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory

from modexbot.config_model import (
    check_model_config,
    get_model_section,
    get_model_value,
    set_model_value,
)


class TestSetGet:
    def test_set_then_get_roundtrip(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "model.yml"
            set_model_value(path, "model", "openai/foo")
            set_model_value(path, "url", "https://api.example.com/v1")
            assert get_model_value(path, "model") == "openai/foo"
            assert get_model_value(path, "url") == "https://api.example.com/v1"

    def test_api_key_stored_as_literal_value(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "model.yml"
            set_model_value(path, "api_key", "sk-secret-literal")
            # Literal value on disk — not an ${ENV} reference.
            raw = path.read_text(encoding="utf-8")
            assert "sk-secret-literal" in raw
            assert "${" not in raw
            assert get_model_value(path, "api_key") == "sk-secret-literal"

    def test_set_preserves_other_keys(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "model.yml"
            set_model_value(path, "model", "openai/foo")
            set_model_value(path, "api_key", "sk-1")
            assert get_model_value(path, "model") == "openai/foo"
            assert get_model_value(path, "api_key") == "sk-1"

    def test_capabilities_list_roundtrip(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "model.yml"
            set_model_value(path, "capabilities", ["text", "image"])
            assert get_model_value(path, "capabilities") == ["text", "image"]

    def test_get_missing_returns_none(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "model.yml"
            assert get_model_value(path, "model") is None
            assert get_model_section(path) == {}


class TestCheckModelConfig:
    def test_complete_when_required_present(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "model.yml"
            set_model_value(path, "model", "openai/foo")
            set_model_value(path, "api_key", "sk-1")
            set_model_value(path, "url", "https://api.example.com/v1")
            complete, missing = check_model_config(path)
            assert complete is True
            assert missing == []

    def test_missing_lists_absent_required_keys(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "model.yml"
            set_model_value(path, "model", "openai/foo")
            complete, missing = check_model_config(path)
            assert complete is False
            assert set(missing) == {"api_key", "url"}

    def test_empty_string_counts_as_missing(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "model.yml"
            set_model_value(path, "model", "openai/foo")
            set_model_value(path, "api_key", "   ")
            set_model_value(path, "url", "https://api.example.com/v1")
            complete, missing = check_model_config(path)
            assert complete is False
            assert missing == ["api_key"]

    def test_template_placeholder_counts_as_missing(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "model.yml"
            # Values copied verbatim from model.example.yml.
            set_model_value(path, "model", "openai/foo")
            set_model_value(path, "api_key", "your_llm_api_key")
            set_model_value(path, "url", "https://api.example.com/v1")
            complete, missing = check_model_config(path)
            assert complete is False
            assert missing == ["api_key"]
