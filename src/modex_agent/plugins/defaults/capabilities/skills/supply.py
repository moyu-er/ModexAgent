"""SkillsSupply — the per-pool owner of ``agent_name -> SkillCatalog``.

Built once per pool (plan §11.3). Main and subagent assembly LOOK UP their
catalog by compiled agent name — they never construct one. Missing
directories produce EMPTY catalogs; they never remove Skills wiring (the
§5.3 correction: the main path never returns ``None``).
"""

from __future__ import annotations

import logging
from pathlib import Path

from modex_agent.commands.skill import SkillResolver
from modex_agent.plugins.capability import CapabilitySupply

from .cache import DirectorySkillCache
from .catalog import SkillCatalog
from .filter import SkillFilter
from .source import FileSkillSource

logger = logging.getLogger(__name__)


def build_skill_catalog(
    skill_roots: list[Path],
    *,
    skill_filter: SkillFilter | None = None,
    with_cache: bool = True,
) -> SkillCatalog:
    """Build one agent's catalog over its disk assignment roots.

    Non-existent roots are included by design: an empty/non-existent root
    simply yields no skills but keeps the catalog (and therefore prompt
    injection and command resolution) wired.
    """
    directories = [Path(d).expanduser().resolve() for d in skill_roots]
    source = FileSkillSource(
        directories=directories,
        cache=True,
        layout="directory",
        skill_filename="SKILL.md",
    )
    cache = DirectorySkillCache(directories=directories, layout="directory") if with_cache else None
    return SkillCatalog(source=source, skill_filter=skill_filter, cache=cache)


class SkillsSupply(CapabilitySupply):
    """The pool-level ``agent_name -> SkillCatalog`` mapping (plan §11.3).

    Regular class: it holds live per-agent catalogs. No background workers
    — the default no-op ``start``/``stop`` from :class:`CapabilitySupply`
    are the whole lifecycle.
    """

    def __init__(
        self,
        *,
        pool_name: str,
        catalog_by_agent: dict[str, SkillCatalog],
    ) -> None:
        self.pool_name = pool_name
        self._catalog_by_agent = dict(catalog_by_agent)

    def catalog_for(self, agent_name: str) -> SkillCatalog:
        """Return the one catalog owned for a capability-effective agent."""
        catalog = self._catalog_by_agent.get(agent_name)
        if catalog is None:
            raise ValueError(
                f"skills capability is not effective for agent {agent_name!r}; "
                "no catalog or resolver is owned for vetoed/external agents"
            )
        return catalog

    def resolver_for(self, agent_name: str) -> SkillResolver:
        """The ONLY factory for bound command resolvers (plan §11.3.1)."""
        return self.catalog_for(agent_name)

    def known_agents(self) -> tuple[str, ...]:
        """The agent names with pre-built catalogs (test/inspection seam)."""
        return tuple(self._catalog_by_agent)


def build_skills_supply(
    *,
    pool_name: str,
    skill_root_for_agent: dict[str, list[Path]],
) -> SkillsSupply:
    """The capability ``supply()`` construction body.

    One catalog per named agent over that agent's assignment roots
    (``skills/<pool>/<agent>/`` — disk is the sole assignment authority,
    plan §11.4).
    """
    catalogs = {
        agent_name: build_skill_catalog(roots)
        for agent_name, roots in skill_root_for_agent.items()
    }
    return SkillsSupply(pool_name=pool_name, catalog_by_agent=catalogs)
