"""Load declarative graph specifications from bot configuration files."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from pathlib import Path

import yaml
from pydantic import ValidationError

from modex_graph import TopologyError
from modex_graph.spec import GraphSpec
from modex_graph.spec_compiler import GraphSpecCompiler
from modex_graph.spec_store import GraphSpecStore

logger = logging.getLogger(__name__)

PoolAgentMap = Mapping[str, frozenset[str]]
"""Maps pool name → set of valid agent names (main + all subagents)."""


def _validate_agent_pool_refs(spec: GraphSpec, pool_agents: PoolAgentMap) -> None:
    """Check that every agent node's config.pool and config.agent exist.

    Raises ``ValueError`` on the first invalid reference. Called after
    topology validation so both structural and semantic errors are caught.
    """
    for node in spec.nodes:
        if node.node_type != "agent":
            continue
        config = node.config
        pool_name = config.get("pool")
        agent_name = config.get("agent")
        if not pool_name or not agent_name:
            continue
        agents = pool_agents.get(pool_name)
        if agents is None:
            raise ValueError(
                f"Node {node.name!r} references pool {pool_name!r} "
                f"which does not exist. Available: {sorted(pool_agents)}."
            )
        if agent_name not in agents:
            raise ValueError(
                f"Node {node.name!r} references agent {agent_name!r} "
                f"in pool {pool_name!r}, but that pool has no agent "
                f"named {agent_name!r}. Available: {sorted(agents)}."
            )


class GraphSpecLoader:
    """Load YAML graph specifications into a graph specification store.

    If a ``compiler`` is provided, topology validation runs alongside
    Pydantic schema validation — invalid-topology specs are skipped with
    a warning, matching the PUT endpoint's validation depth (rule 15:
    converge validation depth across boot and PUT paths).

    If ``pool_agents`` is provided, each agent node's ``config.pool`` and
    ``config.agent`` are checked against real pool configuration — a spec
    referencing a non-existent pool or agent is rejected.
    """

    def __init__(
        self,
        spec_store: GraphSpecStore,
        compiler: GraphSpecCompiler | None = None,
        pool_agents: PoolAgentMap | None = None,
    ) -> None:
        self._spec_store = spec_store
        self._compiler = compiler
        self._pool_agents = pool_agents

    def validate(self, spec: GraphSpec) -> None:
        """Run topology + semantic validation on a spec.

        Raises ``TopologyError`` for structural issues, ``ValueError``
        for invalid pool/agent references.
        """
        if self._compiler is not None:
            self._compiler.validate(spec)
        if self._pool_agents is not None:
            _validate_agent_pool_refs(spec, self._pool_agents)

    def load_from_dir(self, dir_path: Path) -> list[GraphSpec]:
        """Load valid ``*.yml`` specifications and skip invalid files.

        After loading, specs that exist in the store but whose YAML files
        are no longer on disk are deleted — the store stays in sync with
        the filesystem.
        """
        disk_names: set[str] = set()
        loaded: list[GraphSpec] = []
        for spec_path in sorted(dir_path.glob("*.yml")):
            try:
                raw_spec = yaml.safe_load(spec_path.read_text(encoding="utf-8"))
                spec = GraphSpec.model_validate(raw_spec)
                self.validate(spec)
            except (OSError, UnicodeError, yaml.YAMLError, ValidationError, TopologyError, ValueError) as exc:
                logger.warning(
                    "Graph spec '%s' failed to load (%s: %s), skipping",
                    spec_path,
                    type(exc).__name__,
                    exc,
                )
                continue
            self._spec_store.save_if_changed(spec)
            disk_names.add(spec.name)
            loaded.append(spec)

        stale = [
            record for record in self._spec_store.list_records()
            if record.name not in disk_names
        ]
        for record in stale:
            logger.info("Removing stale graph spec '%s' (no YAML file on disk)", record.name)
            self._spec_store.delete(record.spec_id)

        return loaded


__all__ = ["GraphSpecLoader", "PoolAgentMap"]
