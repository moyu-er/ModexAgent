"""Declaration-road test driver — build boot products from inline YAML.

Ticket 11's common test seam: every test that used to construct a legacy
``PoolSpec``/``MainAgentSpec``/``SubagentSpec`` and call ``create_pool`` (or
``AgentTemplate``) now declares its pool as scope YAML and boots the real
declaration road. This helper writes the YAML to a temp file, runs the REAL
production boot (load → validate → compile), and hands back the
``DeclaredPoolBuild`` ``create_pool`` consumes.
"""

from __future__ import annotations

from pathlib import Path

from bot.service.pool.declaration import (
    DeclaredPoolBuild,
    boot_scope_declaration,
    declared_pool_build,
)

DEFAULT_LLM_PROVIDER = "bot_default"


def boot_from_yaml(
    declaration_yaml: str,
    *,
    project_dir: Path,
    data_dir: Path,
) -> object:
    """Write the YAML to ``<data_dir>/declaration.yml`` and boot it.

    Returns the :class:`bot.service.pool.declaration.ScopeBoot` products.
    """
    data_dir.mkdir(parents=True, exist_ok=True)
    declaration_path = data_dir / "declaration.yml"
    declaration_path.write_text(declaration_yaml, encoding="utf-8")
    return boot_scope_declaration(
        declaration_path=declaration_path,
        project_dir=project_dir,
        data_dir=data_dir,
        graphs_dirs=(),
        default_llm_provider=DEFAULT_LLM_PROVIDER,
    )


def build_declared(
    declaration_yaml: str,
    *,
    project_dir: Path,
    data_dir: Path,
    pool_name: str = "default",
) -> DeclaredPoolBuild:
    """Boot the declaration and partition ``pool_name``'s products.

    A convenience for the overwhelmingly common single-pool test shape.
    """
    boot = boot_from_yaml(
        declaration_yaml, project_dir=project_dir, data_dir=data_dir
    )
    return declared_pool_build(boot, pool_name)


def compiled_spec_of(
    agent_yaml_tree: str,
    *,
    project_dir: Path,
    data_dir: Path,
    agent_name: str,
) -> object:
    """Compile ONE declared agent through the real compiler.

    ``agent_yaml_tree`` is the nested ``agents:`` mapping text of a single
    pool (pool-as-root form); returns the agent's compiled
    :class:`modex_agent.plugins.assembly.spec.AssemblySpec` — the
    ``compiled_spec`` an ``AgentTemplate`` needs for materialization.
    """

    boot = boot_from_yaml(
        agent_yaml_tree, project_dir=project_dir, data_dir=data_dir
    )
    for compiled in boot.compilation.agents:
        if compiled.provenance.agent == agent_name:
            return compiled.spec
    raise AssertionError(f"agent {agent_name!r} not found in declaration")
