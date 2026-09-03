from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
COMMANDS = ROOT / "src" / "modex_agent" / "commands"
SKILLS = ROOT / "src" / "modex_agent" / "plugins" / "defaults" / "capabilities" / "skills"
BOT_STAGE = (
    ROOT
    / "examples"
    / "bot_project"
    / "bot"
    / "input_pipeline"
    / "stages"
    / "skill_parse.py"
)


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module)
        elif isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
    return imports


def test_commands_package_does_not_import_plugins() -> None:
    offenders = {
        str(path.relative_to(ROOT)): sorted(
            name for name in _imports(path) if name.startswith("modex_agent.plugins")
        )
        for path in COMMANDS.glob("*.py")
        if any(name.startswith("modex_agent.plugins") for name in _imports(path))
    }
    assert offenders == {}


def test_bot_skill_stage_uses_only_consumer_owned_resolver_seam() -> None:
    framework_imports = {
        name for name in _imports(BOT_STAGE) if name.startswith("modex_agent")
    }
    forbidden = {
        name
        for name in framework_imports
        if name.startswith("modex_agent.plugins")
        or name.startswith("modex_agent.multi_agent")
    }
    assert "modex_agent.commands.skill" in framework_imports
    assert forbidden == set()


def test_skills_package_has_no_multi_agent_import() -> None:
    offenders = {
        str(path.relative_to(ROOT)): sorted(
            name for name in _imports(path) if name.startswith("modex_agent.multi_agent")
        )
        for path in SKILLS.glob("*.py")
        if any(name.startswith("modex_agent.multi_agent") for name in _imports(path))
    }
    assert offenders == {}
