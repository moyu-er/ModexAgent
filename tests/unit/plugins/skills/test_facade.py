from __future__ import annotations

import subprocess
import sys


def test_commands_skill_import_does_not_load_plugins() -> None:
    probe = (
        "import sys\n"
        "import modex_agent.commands.skill\n"
        "hits = [m for m in sys.modules if m.startswith('modex_agent.plugins')]\n"
        "assert not hits, hits\n"
    )
    subprocess.run([sys.executable, "-c", probe], check=True, capture_output=True, text=True)


def test_skills_facade_is_import_light_and_avoids_multi_agent() -> None:
    probe = (
        "import sys\n"
        "import modex_agent\n"
        "before = {m for m in sys.modules if m.startswith('modex_agent.multi_agent')}\n"
        "import modex_agent.plugins.defaults.capabilities.skills\n"
        "prefix = 'modex_agent.plugins.defaults.capabilities.skills.'\n"
        "heavy = {'builder', 'cache', 'catalog', 'filter', 'models', 'section', "
        "'source', 'supply'}\n"
        "hits = [m for m in sys.modules if m.startswith(prefix) "
        "and m.rsplit('.', 1)[-1] in heavy]\n"
        "assert not hits, hits\n"
        "cycles = [m for m in sys.modules if m.startswith('modex_agent.multi_agent') "
        "and m not in before]\n"
        "assert not cycles, cycles\n"
    )
    subprocess.run([sys.executable, "-c", probe], check=True, capture_output=True, text=True)


def test_skills_facade_exports_only_registration_and_supply_access() -> None:
    probe = (
        "import modex_agent.plugins.defaults.capabilities.skills as skills\n"
        "expected = [\n"
        "    'SKILLS_CAPABILITY_NAME',\n"
        "    'SkillsSupply',\n"
        "    'register_skills_feature',\n"
        "    'require_skills_supply',\n"
        "]\n"
        "assert skills.__all__ == expected, skills.__all__\n"
        "try:\n"
        "    from modex_agent.plugins.defaults.capabilities.skills import SkillCatalog\n"
        "except ImportError:\n"
        "    pass\n"
        "else:\n"
        "    raise AssertionError('SkillCatalog leaked through the package facade')\n"
    )
    subprocess.run([sys.executable, "-c", probe], check=True, capture_output=True, text=True)
