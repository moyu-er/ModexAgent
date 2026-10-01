"""FW graph spec loader -- YAML -> GraphSpec -> GraphSpecStore.

Migrated from ``examples/bot_project/bot/graph/spec_loader.py`` (SPEC section
8.5: GraphSpecLoader = FW, generic). Handles:

- YAML parsing (``*.yml`` files from a directory).
- ``GraphSpec`` construction (Pydantic validation).
- Optional topology validation via ``GraphSpecCompiler.validate`` (when a
  compiler is injected).
- Content-deduplicated persistence via ``GraphSpecStore.save_if_changed``.
- Stale-spec cleanup (specs in the store but no longer on disk are deleted).

BIZ-specific concerns (pool/agent reference validation,
``BotAgentNodeFactory`` registration, ``WebUIGraphOutputAdapter`` wiring)
stay in ``examples/bot_project/bot/graph/`` and are NOT handled here.
"""

from __future__ import annotations

import logging
from pathlib import Path

import yaml
from pydantic import ValidationError

from modex_graph import GraphSpec, GraphSpecCompiler, GraphSpecStore, TopologyError

logger = logging.getLogger(__name__)


class GraphSpecLoader:
    """Load YAML graph specifications into a ``GraphSpecStore``.

    If a ``compiler`` is provided, topology validation runs alongside
    Pydantic schema validation -- invalid-topology specs are skipped with
    a warning, matching the PUT endpoint's validation depth (rule 15:
    converge validation depth across boot and PUT paths).
    """

    def __init__(
        self,
        spec_store: GraphSpecStore,
        compiler: GraphSpecCompiler | None = None,
    ) -> None:
        self._spec_store = spec_store
        self._compiler = compiler

    def validate(self, spec: GraphSpec) -> None:
        """Run topology validation on a spec.

        Raises ``TopologyError`` for structural issues. No-op when no
        compiler was injected.
        """
        if self._compiler is not None:
            self._compiler.validate(spec)

    def load_from_dir(self, dir_path: Path) -> list[GraphSpec]:
        """Load valid ``*.yml`` specifications and skip invalid files.

        After loading, specs that exist in the store but whose YAML files
        are no longer on disk are deleted -- the store stays in sync with
        the filesystem.
        """
        disk_names: set[str] = set()
        loaded: list[GraphSpec] = []
        for spec_path in sorted(dir_path.glob("*.yml")):
            try:
                raw_spec = yaml.safe_load(spec_path.read_text(encoding="utf-8"))
                spec = GraphSpec.model_validate(raw_spec)
                self.validate(spec)
            except (
                OSError,
                UnicodeError,
                yaml.YAMLError,
                ValidationError,
                TopologyError,
                ValueError,
            ) as exc:
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
            record
            for record in self._spec_store.list_records()
            if record.name not in disk_names
        ]
        for record in stale:
            logger.info(
                "Removing stale graph spec '%s' (no YAML file on disk)",
                record.name,
            )
            self._spec_store.delete(record.spec_id)

        return loaded


__all__ = ["GraphSpecLoader"]
