"""Regression test for knowledge template init on workspace restore.

Root cause this locks down: ``create_pool`` previously never initialized
knowledge defaults at pool-creation time. Knowledge files were created
lazily on the first ``get_knowledge`` using the *relative*
``default_templates_dir`` from config, which resolves against CWD. When the
workspace is **restored at startup to a non-home dir** (e.g. E:\\download\\bot),
restore runs before pool creation and before ``os.chdir`` lands the CWD on the
restored dir — so the relative ``templates/knowledge`` path resolves against
the restored workspace (where no templates exist) and EMPTY knowledge files
get written.

The fix: ``ensure_long_term_defaults`` resolves the template dir to an
ABSOLUTE path (via ``project_dir``) before calling ``ensure_defaults``, so the
correct templates are found regardless of CWD.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from framework.ioc.configs.memory import MemoryConfig
from framework.ioc.factories.memory import create_memory
from framework.memory.core.scope import MemoryContext
from bot.service.pool_builder import ensure_long_term_defaults


def _write_templates(project_dir: Path) -> Path:
    templates = project_dir / "templates" / "knowledge"
    templates.mkdir(parents=True, exist_ok=True)
    (templates / "SOUL.md").write_text("# Template SOUL\nidentity content\n", encoding="utf-8")
    (templates / "USER.md").write_text("# Template USER\nprofile content\n", encoding="utf-8")
    (templates / "MEMORY.md").write_text("# Template MEMORY\nfacts content\n", encoding="utf-8")
    return templates


def _cfg(scope: str = "global") -> MemoryConfig:
    return MemoryConfig(
        knowledge={"enabled": True, "default_templates_dir": "templates/knowledge", "scope": scope},
    )


@pytest.mark.asyncio
async def test_knowledge_populated_even_when_cwd_is_not_project(tmp_path: Path) -> None:
    """Templates resolve via project_dir, not CWD — restored workspaces get content."""
    project_dir = tmp_path / "project"
    _write_templates(project_dir)

    # Simulate restored non-home workspace: CWD is the workspace dir, NOT project_dir.
    workspace = tmp_path / "ws_restored"
    workspace.mkdir()
    original_cwd = Path.cwd()
    os.chdir(workspace)
    try:
        cfg = _cfg("global")
        ms = create_memory(cfg, None, workspace / ".modex" / "memory" / "main")  # type: ignore[arg-type]
        await ms.initialize()
        await ensure_long_term_defaults(project_dir, cfg, ms)
    finally:
        os.chdir(original_cwd)

    ctx = MemoryContext(session_id="default", user_id="default")
    km = await ms.get_knowledge(ctx)
    assert km.soul == "# Template SOUL\nidentity content\n", (
        f"SOUL must come from template via absolute path, got {km.soul!r}"
    )
    assert km.user == "# Template USER\nprofile content\n"
    assert km.memory == "# Template MEMORY\nfacts content\n"


@pytest.mark.asyncio
async def test_does_not_overwrite_existing_nonempty(tmp_path: Path) -> None:
    """ensure_defaults must skip files that already have non-empty content."""
    project_dir = tmp_path / "project"
    _write_templates(project_dir)

    workspace = tmp_path / "ws"
    cfg = _cfg("global")
    ms = create_memory(cfg, None, workspace)  # type: ignore[arg-type]
    await ms.initialize()

    # Pre-seed a non-empty SOUL.md that the user already customized.
    from framework.memory.core.consolidation import MemoryUpdate, MemoryUpdateMode

    ctx = MemoryContext(session_id="default", user_id="default")
    await ms.knowledge_manager.apply_update(
        ctx,
        MemoryUpdate(
            file_name="SOUL.md",
            content="# MY CUSTOM SOUL\n",
            mode=str(MemoryUpdateMode.SECTION_REPLACE),
            reason="preset",
        ),
    )

    await ensure_long_term_defaults(project_dir, cfg, ms)
    km = await ms.get_knowledge(ctx)
    assert km.soul == "# MY CUSTOM SOUL\n", "existing non-empty SOUL must not be overwritten"


@pytest.mark.asyncio
async def test_noop_when_knowledge_disabled(tmp_path: Path) -> None:
    """knowledge disabled → helper is a no-op, no files written."""
    project_dir = tmp_path / "project"
    _write_templates(project_dir)

    workspace = tmp_path / "ws"
    cfg = MemoryConfig()  # knowledge disabled by default
    ms = create_memory(cfg, None, workspace)  # type: ignore[arg-type]
    await ms.initialize()
    await ensure_long_term_defaults(project_dir, cfg, ms)

    ctx = MemoryContext(session_id="default", user_id="default")
    km = await ms.get_knowledge(ctx)
    # knowledge layer absent → empty LongTermMemory
    assert km.soul == "" and km.user == "" and km.memory == ""
