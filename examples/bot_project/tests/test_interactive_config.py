"""Tests for modexbot.interactive_config — multi-provider model wizard."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent))

from modexbot import interactive_config as ic  # noqa: E402
from modexbot.config_model import load_models_section, save_models_section  # noqa: E402


def _seq(values: list) -> MagicMock:
    """Mock questionary call-chain whose ``.ask()`` yields *values* in order.

    questionary is called as ``questionary.<kind>(...).ask()``. This helper
    returns a MagicMock whose ``ask`` side_effect pops the next value per call.
    """
    it = iter(values)
    m = MagicMock()
    m.ask.side_effect = lambda: next(it)
    return m


def test_wizard_adds_provider(tmp_path: Path) -> None:
    """Add provider -> set default -> exit writes the provider and default."""
    p = tmp_path / "model.yml"
    save_models_section(p, {"default_provider": "", "default_model": "", "providers": []})

    # questionary.select calls, in exact call order:
    #   menu (iter 1)            -> "Add provider"
    #   menu (iter 2)            -> "Set default model"
    #   _set_default: provider   -> "MiniMax"
    #   _set_default: model      -> "M1"
    #   menu (iter 3)            -> "Exit (done editing)"
    ic.questionary.select = MagicMock(side_effect=[
        _seq(["Add provider"]),
        _seq(["Set default model"]),
        _seq(["MiniMax"]),
        _seq(["M1"]),
        _seq(["Exit (done editing)"]),
    ])
    ic.questionary.text = MagicMock(side_effect=[
        _seq(["minimax"]),                      # provider key
        _seq(["MiniMax"]),                      # provider display name
        _seq(["https://api.minimaxi.com/v1"]),  # base url
        _seq(["M1"]),                           # first model name
        _seq(["openai/MiniMax-M1"]),            # model string
    ])
    ic.questionary.password = MagicMock(return_value=_seq(["KEY"]))
    ic.questionary.checkbox = MagicMock(return_value=_seq(["text", "image"]))

    ic.run_config_wizard(p)

    section = load_models_section(p)
    assert section["providers"][0]["name"] == "MiniMax"
    assert section["default_provider"] == "MiniMax"
    assert section["default_model"] == "M1"


def test_wizard_exit_without_changes(tmp_path: Path) -> None:
    """Exiting immediately leaves the existing config untouched."""
    p = tmp_path / "model.yml"
    save_models_section(
        p,
        {
            "default_provider": "A",
            "default_model": "M1",
            "providers": [
                {
                    "key": "a", "name": "A", "url": "u", "api_key": "k",
                    "models": [{"name": "M1", "model": "m1"}],
                }
            ],
        },
    )

    ic.questionary.select = MagicMock(side_effect=[_seq(["Exit (done editing)"])])
    ic.questionary.text = MagicMock()
    ic.questionary.password = MagicMock()
    ic.questionary.checkbox = MagicMock()

    ic.run_config_wizard(p)

    assert load_models_section(p)["providers"][0]["name"] == "A"
