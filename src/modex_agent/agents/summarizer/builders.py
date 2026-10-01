"""Summarizer-agent builders — the concrete-agent construction face.

Moved from ``memory/assembly.py`` (W3b): the concrete summarizer agents are
ReAct-backed (they build on :class:`ScopedFileAgent` →
:class:`~modex_agent.agents.react.agent.ReActAgent`), so their constructors
live above the memory package, in the agents package that owns them. The
memory side consumes the agents through the ABCs in
:mod:`modex_agent.memory.summarizer`; assembly callers above both packages
(plugins, multi-agent wiring) import the builders from here.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from modex_agent.core.provider import LLMProvider
from modex_agent.memory.config import MemoryConfig
from modex_agent.memory.token_estimator import TokenEstimator

if TYPE_CHECKING:
    from modex_agent.agents.summarizer.archive_agent import ArchiveSummarizer
    from modex_agent.agents.summarizer.consolidator import CoreMemoryConsolidator
    from modex_agent.agents.summarizer.session_compactor import SessionCompactorAgent

logger = logging.getLogger(__name__)


def build_session_compactor(
    cfg: MemoryConfig,
    llm_provider: LLMProvider | None,
    token_estimator: TokenEstimator | None = None,
) -> SessionCompactorAgent | None:
    """Build the session compactor from ``cfg.compact`` — shared by the main
    memory factory and the subagent session-only memory builder.

    Returns ``None`` (with a warning) when compaction is enabled but no
    provider is configured — cleanup then degrades to tail-only.
    """
    from modex_agent.agents.summarizer.session_compactor import (
        SessionCompactorAgent,
        SessionCompactorConfig,
    )

    if cfg.compact is None or not cfg.compact.enabled:
        return None
    if llm_provider is None:
        logger.warning(
            "llm_provider is None — skipping session compactor "
            "(no model configured). Cleanup will run in degraded mode "
            "(tail-only, no compact summary)."
        )
        return None
    compact_cfg = SessionCompactorConfig(
        max_output_tokens=cfg.compact.max_output_tokens,
        max_iterations=cfg.compact.max_iterations,
        temperature=cfg.compact.temperature,
        tool_output_max_chars=cfg.compact.tool_output_max_chars,
    )
    return SessionCompactorAgent(
        llm_provider, config=compact_cfg, token_estimator=token_estimator
    )


def build_archive_agents(
    cfg: MemoryConfig,
    llm_provider: LLMProvider | None,
) -> tuple[ArchiveSummarizer | None, CoreMemoryConsolidator | None]:
    """Build the summarizer-agent archive pair (ArchiveSummarizer,
    CoreMemoryConsolidator) from ``cfg.archive`` / ``cfg.core`` /
    ``cfg.summarizer_agent``.

    Returns ``(None, None)`` (with a warning) when archive flow is
    enabled but no provider is configured — memory then runs in degraded
    mode until a model is configured. The consolidator is only built when
    the core layer is enabled alongside the archive flow.
    """
    archive_enabled = cfg.archive is not None and cfg.archive.enabled
    summarizer_enabled = cfg.summarizer_agent is not None and cfg.summarizer_agent.enabled
    if not (archive_enabled or summarizer_enabled):
        return None, None

    from modex_agent.agents.summarizer.archive_agent import (
        ArchiveSummarizer,
        ArchiveSummarizerConfig,
    )
    from modex_agent.agents.summarizer.consolidator import CoreMemoryConsolidator

    if cfg.summarizer_agent is not None:
        archive_config = ArchiveSummarizerConfig(
            context_max_chars=cfg.summarizer_agent.context_max_chars,
            core_max_chars=cfg.summarizer_agent.core_max_chars,
            max_iterations=cfg.summarizer_agent.max_iterations,
        )
        max_iterations = cfg.summarizer_agent.max_iterations
    else:
        archive_config = ArchiveSummarizerConfig()
        max_iterations = ArchiveSummarizerConfig().max_iterations

    if llm_provider is None:
        logger.warning(
            "llm_provider is None — skipping archive summarizer and "
            "core memory consolidator (no model configured). Memory runs "
            "in degraded mode until a model is configured."
        )
        return None, None

    archive_agent = ArchiveSummarizer(llm_provider, config=archive_config)

    core_memory_consolidator = None
    core_enabled = cfg.core is not None and cfg.core.enabled
    if core_enabled:
        core_memory_consolidator = CoreMemoryConsolidator(
            provider=llm_provider,
            max_iterations=max_iterations,
        )
    return archive_agent, core_memory_consolidator
