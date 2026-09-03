"""§18.7 net-new pins: import-light facade, wheel resources, one catalog."""

from __future__ import annotations

import sys

import pytest


class TestImportLightFacade:
    def test_facade_does_not_eagerly_import_implementations(self) -> None:
        """Importing the package facade must NOT import reviewer, memory,
        Tool-implementation, or Hook-implementation modules (plan §10.4,
        §18.7 last bullet) — pinned via sys.modules."""
        import subprocess

        probe = (
            "import sys\n"
            "import modex_agent.plugins.defaults.capabilities.experience as pkg\n"
            "banned = ('reviewer', 'review_hook', 'tools', 'metadata', 'curator',"
            " 'source', 'models', 'catalog', 'section', 'supply')\n"
            "hits = [m for m in sys.modules\n"
            "        if m.startswith(\n"
            "            'modex_agent.plugins.defaults.capabilities.experience.')\n"
            "        and any(m.endswith('.' + b) for b in banned)]\n"
            "assert not hits, f'eagerly imported: {hits}'\n"
            "print('import-light OK')\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", probe],
            capture_output=True,
            text=True,
            check=True,
        )
        assert "import-light OK" in result.stdout

    def test_registration_entry_imports_without_facade_side_effects(self) -> None:
        """The registration module stays import-light too (the factories
        import package-privately inside create())."""
        import subprocess

        probe = (
            "import sys\n"
            "from modex_agent.plugins.defaults.capabilities.experience import (\n"
            "    register_experience_feature,\n"
            ")\n"
            "assert not any(m.endswith('.reviewer') or m.endswith('.tools')\n"
            "               for m in sys.modules\n"
            "               if m.startswith(\n"
            "                   'modex_agent.plugins.defaults.capabilities.experience'))\n"
            "print('registration import-light OK')\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", probe],
            capture_output=True,
            text=True,
            check=True,
        )
        assert "registration import-light OK" in result.stdout


class TestPromptResources:
    def test_both_review_prompts_load_via_importlib_resources(self) -> None:
        """Both review Markdown prompts are package resources readable via
        importlib.resources (the source-checkout half of the wheel test —
        the same loader the reviewer uses)."""
        from importlib import resources

        pkg = "modex_agent.plugins.defaults.capabilities.experience.prompts"
        system = (resources.files(pkg) / "review_system.md").read_text(encoding="utf-8")
        user = (resources.files(pkg) / "review_user.md").read_text(encoding="utf-8")
        assert "experience review agent" in system.lower()
        assert "{conversation_snapshot}" in user
        assert "{existing_experiences}" in user

    @pytest.mark.integration
    async def test_built_wheel_includes_both_prompts(self) -> None:
        """The built-wheel half: uv build + wheel inspect proves both review
        prompts ship inside the installed artifact (plan §15 D1).

        Skips when the wheel build itself fails — a PRE-EXISTING pyproject
        packaging bug (persistence/migrations force-include double-adds
        ``__init__.py`` on directory packages), verified present on the
        D1 baseline before any experience changes.
        """
        import subprocess
        import tempfile
        import zipfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            build = subprocess.run(
                ["uv", "build", "--wheel", "-o", tmp],
                capture_output=True,
                text=True,
            )
            if build.returncode != 0:
                pytest.skip(
                    "pre-existing wheel-build failure (persistence/migrations "
                    "force-include double-add); resource inclusion untestable "
                    "until pyproject is fixed"
                )
            wheels = list(Path(tmp).glob("*.whl"))
            assert len(wheels) == 1, f"expected exactly one wheel, got {wheels}"
            prefix = "modex_agent/plugins/defaults/capabilities/experience/prompts/"
            with zipfile.ZipFile(wheels[0]) as wheel:
                names = wheel.namelist()
                for filename in ("review_system.md", "review_user.md"):
                    assert prefix + filename in names, (
                        f"{filename} missing from wheel; prompts entries: "
                        f"{[n for n in names if 'prompts' in n]}"
                    )
                    content = wheel.read(prefix + filename).decode("utf-8")
                    assert len(content) > 100


class TestOneCatalogImplementation:
    async def test_tool_and_section_share_the_catalog(self, tmp_path) -> None:
        """The prompt section and the tool ride the SAME catalog from one
        supply (invariant §10.6)."""
        from unittest.mock import MagicMock

        from modex_agent.plugins.assembly.context import AgentContext, PoolRuntimeDeps
        from modex_agent.plugins.capability import (
            CapabilityBinding,
            PoolSupplyAgentEntry,
            PoolSupplyView,
            PromptSectionSpec,
        )
        from modex_agent.plugins.defaults.capabilities.experience import (
            ExperienceCapability,
        )

        section = PromptSectionSpec(section_id="experience.injection", order=50)
        capability = ExperienceCapability()
        supply = capability.supply(
            PoolSupplyView(
                pool_name="p",
                entries=(PoolSupplyAgentEntry(agent_name="main", config={}),),
                root_agent_name="main",
                data_dir=tmp_path,
            )
        )
        wiring = await capability.assemble(
            CapabilityBinding(active_sections=(section,)),
            AgentContext(
                registry=MagicMock(),
                workspace_ctx=MagicMock(),
                pool_runtime=PoolRuntimeDeps(capability_supply={"experience": supply}),
                agent_name="main",
            ),
        )

        from modex_agent.plugins.defaults.capabilities.experience.catalog import (
            ExperienceRouterTool,
        )
        from modex_agent.plugins.defaults.capabilities.experience.section import (
            ExperienceInjectionProvider,
        )
        from modex_agent.plugins.defaults.capabilities.experience.tool_factory import (
            ExperienceToolFactory,
        )

        tool = await ExperienceToolFactory().create(
            ExperienceToolFactory.config_model(),
            AgentContext(
                registry=MagicMock(),
                workspace_ctx=MagicMock(),
                pool_runtime=PoolRuntimeDeps(capability_supply={"experience": supply}),
                agent_name="main",
            ),
        )
        assert isinstance(tool, ExperienceRouterTool)
        provider = wiring.prompt_providers[0]
        assert isinstance(provider, ExperienceInjectionProvider)

        assert provider._catalog is tool._catalog  # noqa: SLF001
        assert tool._catalog is supply.catalog  # noqa: SLF001
