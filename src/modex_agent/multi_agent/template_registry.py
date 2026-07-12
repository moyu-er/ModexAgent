"""AgentTemplateRegistry — loads per-pool subagent templates via PoolStore."""

from __future__ import annotations

import logging

from modex_agent.ioc.configs.memory import MemoryConfig
from modex_agent.multi_agent.pool_config.store import PoolStore
from modex_agent.multi_agent.template import AgentTemplate

logger = logging.getLogger(__name__)


class AgentTemplateRegistry:
    """Loads per-pool subagent templates through :class:`PoolStore`.

    Templates are isolated by pool_name — a template only exists within
    the pool directory it's defined in. All YAML parsing and validation is
    delegated to ``PoolStore``; this registry wraps each ``SubagentSpec``
    into an ``AgentTemplate`` and applies the caller's default subagent memory.
    """

    def __init__(
        self,
        pool_store: PoolStore,
        *,
        default_subagent_memory: MemoryConfig | None = None,
    ) -> None:
        """Init.

        ``default_subagent_memory`` is baked onto EVERY subagent template,
        unconditionally (spec §9 — sub-minimal, immutable). A template may NOT
        carry its own ``memory:`` block; the caller's factory is the single
        source of truth.
        """
        self._default_memory = default_subagent_memory
        self._templates: dict[str, dict[str, AgentTemplate]] = {}
        self._load(pool_store)

    def _load(self, pool_store: PoolStore) -> None:
        for summary in pool_store.list_pools():
            pool_name = summary.name
            try:
                pool_spec = pool_store.read_pool(pool_name)
            except Exception:
                logger.exception("Failed to read pool %s", pool_name)
                continue

            self._templates[pool_name] = {}
            for sub_spec in pool_spec.subagents:
                try:
                    template = AgentTemplate(
                        spec=sub_spec,
                        memory=self._default_memory,
                    )
                    self._templates[pool_name][template.spec.agent_name] = template
                    logger.debug(
                        "Loaded template %s for pool %s",
                        template.spec.agent_name,
                        pool_name,
                    )
                except Exception:
                    logger.exception(
                        "Failed to load template for subagent %s in pool %s",
                        sub_spec.agent_name,
                        pool_name,
                    )

    def list_templates(self, pool_name: str) -> list[AgentTemplate]:
        return list(self._templates.get(pool_name, {}).values())

    def get_template(self, pool_name: str, agent_name: str) -> AgentTemplate | None:
        return self._templates.get(pool_name, {}).get(agent_name)
