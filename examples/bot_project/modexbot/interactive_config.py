# modexbot/interactive_config.py
"""Interactive multi-provider/multi-model config wizard for config/model.yml."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import questionary
from rich.console import Console
from rich.table import Table

from modex_agent.ioc.configs.llm import InterfaceFormat
from modexbot.config_model import (
    load_models_section,
    save_models_section,
)

_CONSOLE = Console()
_CAPABILITY_CHOICES = ("text", "image", "video", "audio")
_SELECT_HINT = "use arrows + enter"
_TEXT_HINT = "type and enter; empty keeps current"
_CHECKBOX_HINT = "space to toggle, enter to confirm"


def _print_summary(section: dict[str, Any]) -> None:
    table = Table(title="Model configuration")
    table.add_column("default")
    table.add_column("providers / models")
    dp, dm = section.get("default_provider"), section.get("default_model")
    lines: list[str] = []
    for p in section.get("providers") or []:
        models = ", ".join(m["name"] for m in p.get("models") or [])
        lines.append(f"{p.get('name')}  [{models}]")
    table.add_row(f"{dp}/{dm}", "\n".join(lines) or "(none)")
    _CONSOLE.print(table)


def _add_provider(p: Path, section: dict[str, Any]) -> None:
    key = questionary.text("provider key:", instruction=f"({_TEXT_HINT})").ask()
    name = questionary.text("provider display name:", instruction=f"({_TEXT_HINT})").ask()
    base_url = questionary.text("base url:", instruction=f"({_TEXT_HINT})").ask()
    api_key = questionary.password("api key:").ask()
    interface_format = questionary.select(
        "interface format:",
        choices=[
            questionary.Choice("OpenAI Compatible", InterfaceFormat.OPENAI_COMPATIBLE.value),
            questionary.Choice("Anthropic", InterfaceFormat.ANTHROPIC.value),
        ],
        default=InterfaceFormat.OPENAI_COMPATIBLE.value,
        instruction=f"({_SELECT_HINT})",
    ).ask()
    if not (key and name and base_url and api_key and interface_format):
        _CONSOLE.print("Missing fields; provider not added.")
        return
    m_name = questionary.text("first model name:", instruction=f"({_TEXT_HINT})").ask()
    m_model = questionary.text("model string:", instruction=f"({_TEXT_HINT})").ask()
    caps = questionary.checkbox(
        "capabilities:", choices=list(_CAPABILITY_CHOICES), instruction=f"({_CHECKBOX_HINT})"
    ).ask() or ["text"]
    provider = {
        "key": key,
        "name": name,
        "base_url": base_url,
        "api_key": api_key,
        "interface_format": interface_format,
        "models": [{"name": m_name, "model": m_model, "capabilities": caps}],
    }
    section.setdefault("providers", []).append(provider)
    if not section.get("default_provider"):
        section["default_provider"] = name
        section["default_model"] = m_name
    save_models_section(p, section)
    _CONSOLE.print(f"[green]Provider {name} added.[/green] Run 'modexbot restart' to apply.")


def _set_default(p: Path, section: dict[str, Any]) -> None:
    providers = [pp["name"] for pp in section.get("providers") or []]
    if not providers:
        _CONSOLE.print("No providers yet.")
        return
    pname = questionary.select(
        "provider:", choices=providers, instruction=f"({_SELECT_HINT})"
    ).ask()
    provider = next(pp for pp in section["providers"] if pp["name"] == pname)
    models = [m["name"] for m in provider.get("models") or []]
    mname = questionary.select("model:", choices=models, instruction=f"({_SELECT_HINT})").ask()
    section["default_provider"] = pname
    section["default_model"] = mname
    save_models_section(p, section)
    _CONSOLE.print(f"[green]Default set to {pname}/{mname}.[/green]")


def run_config_wizard(model_path: Path) -> None:
    """Interactive wizard for config/model.yml (models: block)."""
    while True:
        section = load_models_section(model_path)
        section.setdefault("providers", [])
        _print_summary(section)
        action = questionary.select(
            "Action:",
            choices=["Add provider", "Set default model", "Exit (done editing)"],
            instruction=f"({_SELECT_HINT})",
        ).ask()
        if action is None or action.startswith("Exit"):
            _CONSOLE.print("Exiting config wizard.")
            return
        if action == "Add provider":
            _add_provider(model_path, section)
        elif action == "Set default model":
            _set_default(model_path, section)


if __name__ == "__main__":
    run_config_wizard(Path(__file__).resolve().parent.parent / "config" / "model.yml")
