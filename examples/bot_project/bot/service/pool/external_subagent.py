"""External subagent builder factory.

Extracted from ``pool_builder.py`` (ADR-0025 ticket 6 split). Constructs a
``BotSubagentExternalBuilder`` iff a pool declares at least one external
subagent.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from modex_agent.core.constants import ExecutionStrategyKind
from modex_agent.multi_agent.pool_config.specs import PoolSpec


def _maybe_build_external_subagent_builder(
    *,
    pool_spec: PoolSpec,
    pool_name: str,
    project_dir: Path,
    data_dir: Path,
    app_config: Any | None,
    persistence: Any | None,
) -> Any | None:
    """Construct a ``BotSubagentExternalBuilder`` iff this pool has external subagents.

    Returns ``None`` for react-only pools so ``AgentMaterializeDeps``
    leaves ``subagent_external_builder=None`` (zero overhead —
    ``AgentTemplate.materialize`` never touches the field on the react
    path). When at least one subagent declares
    ``execution_strategy=EXTERNAL``, returns a pool-scoped builder
    that per-invocation assembles a fully-wired ``ExternalAgent``
    subagent (T8).
    """
    has_external = any(
        sub.execution_strategy == ExecutionStrategyKind.EXTERNAL for sub in pool_spec.subagents
    )
    if not has_external:
        return None
    from bot.service.subagent_external_builder import (
        BotSubagentExternalBuilder,
    )

    return BotSubagentExternalBuilder(
        pool_name=pool_name,
        project_dir=project_dir,
        data_dir=data_dir,
        app_config=app_config,
        persistence=persistence,
    )
