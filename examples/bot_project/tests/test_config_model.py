# tests/test_config_model.py
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from modexbot.config_model import (
    add_provider,
    check_model_config,
    load_models_section,
    save_models_section,
    set_default_model,
)

_PLACEHOLDER = "your_api_key"


def _empty(tmp_path: Path) -> Path:
    p = tmp_path / "model.yml"
    p.write_text("", encoding="utf-8")
    return p


def test_round_trip_models_section(tmp_path: Path) -> None:
    p = _empty(tmp_path)
    save_models_section(
        p,
        {
            "default_provider": "A",
            "default_model": "M1",
            "providers": [
                {"key": "a", "name": "A", "url": "u", "api_key": "k",
                 "models": [{"name": "M1", "model": "m1"}]}
            ],
        },
    )
    section = load_models_section(p)
    assert section["default_provider"] == "A"
    assert section["providers"][0]["models"][0]["name"] == "M1"


def test_add_provider_appends(tmp_path: Path) -> None:
    p = _empty(tmp_path)
    save_models_section(
        p,
        {
            "default_provider": "A",
            "default_model": "M1",
            "providers": [
                {"key": "a", "name": "A", "url": "u", "api_key": "k",
                 "models": [{"name": "M1", "model": "m1"}]}
            ],
        },
    )
    add_provider(p, {"key": "b", "name": "B", "url": "u2", "api_key": "k2", "models": []})
    names = [pp["name"] for pp in load_models_section(p)["providers"]]
    assert names == ["A", "B"]


def test_set_default_model(tmp_path: Path) -> None:
    p = _empty(tmp_path)
    save_models_section(
        p,
        {
            "default_provider": "A",
            "default_model": "M1",
            "providers": [
                {"key": "a", "name": "A", "url": "u", "api_key": "k",
                 "models": [{"name": "M1", "model": "m1"}, {"name": "M2", "model": "m2"}]}
            ],
        },
    )
    set_default_model(p, "A", "M2")
    s = load_models_section(p)
    assert (s["default_provider"], s["default_model"]) == ("A", "M2")


def test_check_model_config_flags_placeholder(tmp_path: Path) -> None:
    p = _empty(tmp_path)
    save_models_section(
        p,
        {
            "default_provider": "A",
            "default_model": "M1",
            "providers": [
                {"key": "a", "name": "A", "url": "u", "api_key": _PLACEHOLDER,
                 "models": [{"name": "M1", "model": "m1"}]}
            ],
        },
    )
    complete, missing = check_model_config(p)
    assert complete is False
    assert "api_key" in missing
