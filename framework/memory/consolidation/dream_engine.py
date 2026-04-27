"""DreamEngine — two-phase offline memory consolidation.

Uses SummarizerAgent exclusively for all LLM calls.
If no SummarizerAgent is provided, one is auto-constructed from llm_provider.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from framework.agents.summarizer.agent import SummarizerAgent
from framework.core.provider import LLMProvider
from framework.memory.core.consolidation import (
    ConsolidationEngine,
    ConsolidationResult,
    MemoryUpdate,
)
from framework.memory.core.layers import ArchiveMemoryManager, KnowledgeMemoryManager
from framework.memory.core.message import ChatMessage
from framework.memory.core.models import ArchiveEntry
from framework.memory.core.scope import (
    MemoryAgentRole,
    MemoryContext,
    MemoryLayerName,
)
from framework.memory.core.storage import MemoryStorage
from framework.memory.registry.base import MemoryStoreRegistry

logger = logging.getLogger(__name__)


def _msg_to_dict(msg: ChatMessage | dict[str, Any]) -> dict[str, Any]:
    """将 ChatMessage 或 dict 统一转为 dict。"""
    return msg.to_dict() if isinstance(msg, ChatMessage) else msg


class DreamEngine(ConsolidationEngine):
    """离线 DreamEngine：两阶段长期记忆整合。

    Phase 1: 使用 SummarizerAgent 分析未处理的历史摘要
    Phase 2: 使用 SummarizerAgent 生成具体的 MemoryUpdate 指令

    所有 LLM 调用统一经过 SummarizerAgent，不再直接使用 llm_provider。
    若未提供 SummarizerAgent，则自动从 llm_provider 构建。
    """

    def __init__(
        self,
        llm_provider: LLMProvider,
        history_manager: ArchiveMemoryManager,
        long_term_manager: KnowledgeMemoryManager,
        max_batch_size: int = 20,
        max_iterations: int = 10,
        storage: MemoryStorage | None = None,
        registry: MemoryStoreRegistry | None = None,
        schedule_mode: str = "manual",
        idle_threshold_entries: int = 5,
        summarizer: SummarizerAgent | None = None,
    ):
        self.history_manager = history_manager
        self.long_term_manager = long_term_manager
        self.max_batch_size = max_batch_size
        self.max_iterations = max_iterations
        self.storage = storage
        self.registry = registry
        self.schedule_mode = schedule_mode
        self.idle_threshold_entries = idle_threshold_entries
        # Always use SummarizerAgent — auto-construct from llm_provider if needed
        self._summarizer: SummarizerAgent = summarizer or SummarizerAgent(llm_provider)

    async def run(self, context: MemoryContext) -> bool:
        """处理未处理的历史条目。

        Returns:
            如果实际处理了条目则返回 True，否则返回 False
        """
        unprocessed = await self.history_manager.get_unprocessed(
            context, cursor_name="dream"
        )
        entries = unprocessed.entries
        if not entries:
            return False

        batch = entries[: self.max_batch_size]
        batch_payload = [self._archive_entry_to_dict(entry) for entry in batch]
        logger.debug(
            "DreamEngine: processing %s entries (cursor %s)",
            len(batch),
            unprocessed.cursor,
        )

        # Filter out meaningless entries before processing
        meaningful = [e for e in batch_payload if self._is_meaningful_entry(e)]
        final_cursor = max((e.entry_id or 0 for e in batch), default=unprocessed.cursor)

        if not meaningful:
            logger.debug("DreamEngine: all entries were empty/meaningless — advancing cursor")
            await self.history_manager.commit_cursor(context, "dream", final_cursor)
            return False

        # Gather existing memories for context
        existing = await self.long_term_manager.get_all(context)
        existing_memories = {
            "SOUL.md": existing.soul,
            "USER.md": existing.user,
            "MEMORY.md": existing.memory,
            **existing.custom,
        }

        result = await self.consolidate(
            scope_key="",
            new_entries=meaningful,
            existing_memories=existing_memories,
        )

        # Apply updates
        if result.success:
            applied = 0
            for update in result.soul_updates + result.user_updates + result.memory_updates:
                await self.long_term_manager.apply_update(context, update)
                applied += 1
            if applied:
                logger.debug("DreamEngine applied %s updates", applied)

        # Always advance cursor to prevent re-processing (even on failure)
        await self.history_manager.commit_cursor(context, "dream", final_cursor)
        logger.debug("DreamEngine cursor advanced to %s", final_cursor)

        return result.success

    async def scan_all(self) -> list[MemoryContext]:
        """扫描 history 层 scope records，返回处理过的 MemoryContext 列表。

        只处理 main agent 的 scope；peer/subagent 被过滤掉。
        每个 scope 调用 run() 处理未处理的 history 条目。
        """
        processed: list[MemoryContext] = []
        if self.registry is None:
            logger.warning("DreamEngine.scan_all skipped: no registry configured")
            return processed

        records = await self.registry.list_records(
            layer=MemoryLayerName.ARCHIVE,
            has_file="history",
            agent_roles={MemoryAgentRole.MAIN},
        )
        for record in records:
            ctx = record.context
            if ctx is None:
                continue
            try:
                did_work = await self.run(ctx)
                if did_work:
                    processed.append(ctx)
            except Exception as e:
                logger.warning(
                    "DreamEngine failed for scope %s: %s", record.scope_key, e
                )
        return processed

    async def consolidate(
        self,
        scope_key: str,
        new_entries: list[dict[str, Any]],
        existing_memories: dict[str, str],
    ) -> ConsolidationResult:
        """整合新历史条目到长期记忆。"""
        _ = scope_key
        if not new_entries:
            return ConsolidationResult.empty()

        dict_entries = [_msg_to_dict(e) for e in new_entries]
        history_text = "\n".join(
            f"[{e.get('timestamp', '?')}] {e.get('summary', e.get('content', ''))}"
            for e in dict_entries
        )

        file_context = (
            f"## Current SOUL.md\n{existing_memories.get('SOUL.md', '(empty)')}\n\n"
            f"## Current USER.md\n{existing_memories.get('USER.md', '(empty)')}\n\n"
            f"## Current MEMORY.md\n{existing_memories.get('MEMORY.md', '(empty)')}"
        )

        # Phase 1: Fact extraction — via SummarizerAgent
        try:
            analysis = await self._summarizer.analyze(
                f"## History\n{history_text}\n\n{file_context}",
                prompt=SummarizerAgent.PROMPT_FACT_EXTRACTION,
                max_tokens=2000,
            )
        except Exception as e:
            logger.warning("DreamEngine Phase 1 failed: %s", e)
            return ConsolidationResult(success=False, reasoning=f"Phase 1 error: {e}")

        if "[SKIP]" in analysis:
            return ConsolidationResult(
                success=True,
                reasoning="Phase 1: no new information",
            )

        # Phase 2: Generate memory updates — via SummarizerAgent
        try:
            phase2_text = await self._summarizer.summarize(
                f"## Analysis\n{analysis}\n\n{file_context}",
                prompt=SummarizerAgent.PROMPT_MEMORY_UPDATE,
                max_tokens=2000,
                temperature=0.2,
            )
            updates = self._parse_updates(phase2_text)
        except Exception as e:
            logger.warning("DreamEngine Phase 2 failed: %s", e)
            return ConsolidationResult(
                success=False,
                reasoning=f"Phase 2 error: {e}",
            )

        result = ConsolidationResult(success=True, reasoning=analysis)
        for update in updates:
            if update.file_name.upper().startswith("SOUL"):
                result.soul_updates.append(update)
            elif update.file_name.upper().startswith("USER"):
                result.user_updates.append(update)
            else:
                result.memory_updates.append(update)

        return result

    @staticmethod
    def _is_meaningful_entry(entry: dict[str, Any]) -> bool:
        """Check whether an archive entry contains useful content for consolidation."""
        summary = entry.get("summary", "")
        if not summary or not summary.strip():
            return False
        return summary.strip() not in ("(no conversation content)", "(no summary)", "(nothing)")

    @staticmethod
    def _archive_entry_to_dict(entry: ArchiveEntry) -> dict[str, Any]:
        return {
            "entry_id": entry.entry_id,
            "summary": entry.summary,
            "metadata": dict(entry.metadata),
            "created_at": entry.created_at.isoformat() if entry.created_at else None,
            "raw_refs": list(entry.raw_refs),
        }

    @staticmethod
    def _parse_updates(response: Any) -> list[MemoryUpdate]:
        """解析 LLM 响应中的 MemoryUpdate 列表。"""
        text = response.strip() if isinstance(response, str) else str(response).strip()
        if not text:
            return []

        # Remove markdown code blocks if present
        if text.startswith("```"):
            lines = text.splitlines()
            if lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].startswith("```"):
                lines = lines[:-1]
            text = "\n".join(lines).strip()

        # Try direct JSON parse first
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            # Fallback: regex extract outermost JSON array
            match = re.search(r"\[.*\]", text, re.DOTALL)
            if match:
                try:
                    data = json.loads(match.group())
                except json.JSONDecodeError:
                    logger.warning("DreamEngine failed to parse updates JSON: %s", text[:200])
                    return []
            else:
                logger.warning("DreamEngine failed to parse updates JSON: %s", text[:200])
                return []

        if not isinstance(data, list):
            return []

        updates: list[MemoryUpdate] = []
        for item in data:
            if isinstance(item, dict):
                updates.append(
                    MemoryUpdate(
                        file_name=item.get("file_name", "MEMORY.md"),
                        content=item.get("content", ""),
                        mode=item.get("mode", "append"),
                        reason=item.get("reason", ""),
                        search_text=item.get("search_text", ""),
                    )
                )
        return updates
