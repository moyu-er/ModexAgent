"""AgentTemplateRegistry — the seeded per-pool subagent template store.

The scope-declaration road (ticket 07+) constructs the registry pre-seeded
with in-memory templates built from the compiled declarations; the
PoolStore disk scan of the legacy ``templates/*.yml`` files died with the
legacy road (ticket 11 — the declaration is the single template source).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping

from modex_agent.multi_agent.template import AgentTemplate

logger = logging.getLogger(__name__)


class AgentTemplateRegistry:
    """Holds per-pool subagent templates, keyed ``pool_name → agent_name``.

    Templates are isolated by pool_name — a template only exists within
    the pool that declared it. The registry is seeded from the compiled
    scope declaration (``declared_pool_build``); there is no disk-scan
    constructor any more.
    """

    def __init__(
        self,
        *,
        seeded: Mapping[str, Mapping[str, AgentTemplate]] | None = None,
    ) -> None:
        """Init."""
        self._templates: dict[str, dict[str, AgentTemplate]] = {}
        if seeded is not None:
            self._templates = {
                pool_name: dict(templates)
                for pool_name, templates in seeded.items()
            }

    def list_templates(self, pool_name: str) -> list[AgentTemplate]:
        return list(self._templates.get(pool_name, {}).values())

    def get_template(self, pool_name: str, agent_name: str) -> AgentTemplate | None:
        return self._templates.get(pool_name, {}).get(agent_name)
