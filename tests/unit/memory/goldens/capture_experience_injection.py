"""Machine-capture the experience injection section's PRE-MIGRATION bytes.

Run from the repository root on the T14 parent commit (the tree where
``MemorySystemContextManager._experience_manager`` still renders the
injection at load() position 8)::

    .venv\\Scripts\\python.exe -m tests.unit.memory.goldens.capture_experience_injection

Writes ``experience_section_pre_migration.txt`` (utf-8, ``newline=""``) —
the exact bytes the retired special case appended to the prompt pipeline
(``ExperienceProvider(await manager.render_prompt(context=ctx))`` with the
BIZ manager construction: a scope-less ``FileExperienceSource``), with the
fixture root normalized to ``<ROOT>`` (the rendered ``directory=""``
attributes embed absolute paths — the normalization is mechanical and is
applied identically to both sides by the post-migration assertion).
Re-running on the same tree must produce a zero diff.

Post-migration, ``tests/unit/plugins/test_experience_supply.py`` asserts the
capability channel (``ExperienceCapability.supply`` → the manager →
``assemble``'s content-hash provider) reproduces these bytes verbatim —
content parity is the acceptance bar; the section's move from position 8 to
the capability anchor (SPEC §7.3) is the documented position delta.
"""

from __future__ import annotations

from pathlib import Path

import anyio

from modex_agent.plugins.defaults.capabilities.experience.catalog import ExperienceCatalog
from modex_agent.plugins.defaults.capabilities.experience.metadata import (
    PerFileExperienceMetaStore,
)
from modex_agent.plugins.defaults.capabilities.experience.source import FileExperienceSource
from modex_agent.core.scope import MemoryContext

_GOLDEN_DIR = Path(__file__).resolve().parent
_GOLDEN_FILE = _GOLDEN_DIR / "experience_section_pre_migration.txt"

# Deterministic fixture set — exercises the builder's XML escaping (quotes,
# ampersands, angle brackets), tags/scenario rendering, empty optional
# fields, and the sorted-by-directory order. Stays under the 20-entry cap.
_FIXTURES: tuple[tuple[str, str, str, str, list[str]], ...] = (
    (
        "alpha-deployment",
        'Deploying with "docker compose" — mind the & <env> escaping',
        "When rolling out the compose stack on a fresh host",
        "run `docker compose up -d` then poll /healthz",
        ["deployment", "docker"],
    ),
    (
        "beta-empty-optional-fields",
        "",
        "",
        "",
        [],
    ),
    (
        "gamma-unicode-quotes",
        "终端交互需要 pexpect — em-dash & “smart quotes”",
        "交互式终端程序卡住等待输入时",
        "send a single stdin line via bash_input instead of re-running",
        ["terminal", "pexpect", "win32"],
    ),
)


def _write_fixtures(root: Path) -> Path:
    exp_dir = root / "experiences" / "pool" / "main"
    for name, description, scenario, trigger, tags in _FIXTURES:
        entry = exp_dir / name
        entry.mkdir(parents=True, exist_ok=True)
        tags_yaml = "[" + ", ".join(tags) + "]" if tags else "[]"
        (entry / "EXPERIENCE.md").write_text(
            "---\n"
            f"name: {name}\n"
            f"description: {description}\n"
            f"tags: {tags_yaml}\n"
            f"scenario: {scenario}\n"
            f"trigger: {trigger}\n"
            "---\n"
            f"# {name}\n\nBody for {name}.\n",
            encoding="utf-8",
        )
    return exp_dir


async def capture_section_bytes(root: Path) -> str:
    """Render the retired channel's section content for the fixture set.

    The fixture root is normalized to ``<ROOT>`` — the builder's
    ``directory=""`` attributes embed the absolute experience dir, which
    differs per machine/run; everything outside the root prefix is the
    verbatim rendering.
    """
    exp_dir = _write_fixtures(root)
    catalog = ExperienceCatalog(
        experience_dir=exp_dir, meta_store=PerFileExperienceMetaStore(exp_dir)
    )
    # The retired load()'s exact call shape: a MemoryContext threads through
    # to the source (a no-op for the scope-less construction, which the
    # golden pins).
    section = await catalog.render_index()
    return section.replace(str(root.resolve()), "<ROOT>")


async def main() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        section = await capture_section_bytes(Path(tmp))
    with _GOLDEN_FILE.open("w", encoding="utf-8", newline="") as stream:
        stream.write(section)
    print(f"wrote {_GOLDEN_FILE} ({len(section.encode('utf-8'))} bytes)")


if __name__ == "__main__":
    anyio.run(main)
