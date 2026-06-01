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
from framework.memory.archive_models import (
    KNOWLEDGE_ARCHIVE_FILE_KEY,
    ArchiveChannel,
)
from framework.memory.core.consolidation import (
    ConsolidationEngine,
    ConsolidationResult,
    MemoryUpdate,
    MemoryUpdateMode,
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


def _file_needs_update(analysis: str, file_key: str) -> bool:
    """Check if analysis contains facts for this file."""
    marker = f"[{file_key.upper()}]"
    return marker in analysis


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
        min_archive_count: int = 0,
        max_archive_count: int = 30,
        prompts: Any = None,  # PromptRegistry (optional)
    ):
        self.history_manager = history_manager
        self.long_term_manager = long_term_manager
        self.max_batch_size = max_batch_size
        self.max_iterations = max_iterations
        self.storage = storage
        self.registry = registry
        self.schedule_mode = schedule_mode
        self.idle_threshold_entries = idle_threshold_entries
        self.min_archive_count = min_archive_count
        self.max_archive_count = max_archive_count
        if prompts is None:
            from framework.memory.prompts import create_default_registry

            try:
                prompts = create_default_registry()
            except Exception:
                pass
        self._prompts = prompts
        # Always use SummarizerAgent — auto-construct from llm_provider if needed
        self._summarizer: SummarizerAgent = summarizer or SummarizerAgent(llm_provider)

    async def run(self, context: MemoryContext) -> bool:
        """处理未处理的历史条目。

        Returns:
            如果实际处理了条目则返回 True，否则返回 False
        """
        unprocessed = await self.history_manager.get_unprocessed(
            context,
            cursor_name="dream",
            channel=ArchiveChannel.KNOWLEDGE,
        )
        entries = unprocessed.entries
        if not entries:
            return False

        archive_count = len(entries)

        scope_info = f"user={context.user_id} agent={context.agent_id}"

        # Dual trigger: skip if below minimum threshold
        if archive_count < self.min_archive_count:
            logger.debug(
                "DreamEngine: skipping consolidation, archive_count=%d < min=%d, %s",
                archive_count,
                self.min_archive_count,
                scope_info,
            )
            return False

        # Dual trigger: force trigger if above maximum threshold
        if archive_count > self.max_archive_count:
            logger.info(
                "DreamEngine: archive overflow trigger, archive_count=%d > max=%d, %s",
                archive_count,
                self.max_archive_count,
                scope_info,
            )

        batch = entries[: self.max_batch_size]
        batch_payload = [self._archive_entry_to_dict(entry) for entry in batch]
        logger.info(
            "DreamEngine: consolidating %d entries into knowledge (cursor %s, total=%d)",
            len(batch),
            unprocessed.cursor,
            archive_count,
        )

        # Filter out meaningless entries before processing
        meaningful = [e for e in batch_payload if self._is_meaningful_entry(e)]
        final_cursor = max((e.entry_id or 0 for e in batch), default=unprocessed.cursor)

        if not meaningful:
            logger.debug("DreamEngine: all entries were empty/meaningless — advancing cursor")
            await self._commit_knowledge_cursor(context, final_cursor)
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
                logger.info(
                    "DreamEngine: knowledge consolidation complete, applied %d updates (soul=%d user=%d memory=%d)",
                    applied,
                    len(result.soul_updates),
                    len(result.user_updates),
                    len(result.memory_updates),
                )

        # Always advance cursor to prevent re-processing (even on failure)
        await self._commit_knowledge_cursor(context, final_cursor)
        logger.debug("DreamEngine cursor advanced to %s", final_cursor)

        return result.success

    async def _commit_knowledge_cursor(
        self,
        context: MemoryContext,
        cursor: int,
    ) -> None:
        await self.history_manager.commit_cursor(
            context,
            "dream",
            cursor,
            channel=ArchiveChannel.KNOWLEDGE,
        )
        await self.history_manager.prune_consumed_pairs(context)

    async def scan_all(self) -> list[MemoryContext]:
        """扫描 history 层 scope records，返回处理过的 MemoryContext 列表。

        只处理 main agent 的 scope；subagent 被过滤掉。
        每个 scope 调用 run() 处理未处理的 history 条目。
        """
        processed: list[MemoryContext] = []
        if self.registry is None:
            logger.warning("DreamEngine.scan_all skipped: no registry configured")
            return processed

        records = await self.registry.list_records(
            layer=MemoryLayerName.ARCHIVE,
            has_file=KNOWLEDGE_ARCHIVE_FILE_KEY,
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
                logger.warning("DreamEngine failed for scope %s: %s", record.scope_key, e)
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

        # Phase 1: Fact extraction
        try:
            if self._prompts is not None:
                system_prompt = self._prompts.get_system("knowledge/fact_extraction")
                user_prompt = self._prompts.get_user(
                    "knowledge/fact_extraction",
                    archive_entries=history_text,
                    current_soul=existing_memories.get("SOUL.md", ""),
                    current_user=existing_memories.get("USER.md", ""),
                    current_memory=existing_memories.get("MEMORY.md", ""),
                )
                analysis = await self._summarizer.analyze(
                    user_prompt,
                    prompt=system_prompt,
                    max_tokens=2000,
                )
            else:
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

        # Phase 2: Per-file updates
        result = ConsolidationResult(success=True, reasoning=analysis)

        file_mapping: dict[str, tuple[str, list[MemoryUpdate]]] = {
            "soul": ("SOUL.md", result.soul_updates),
            "user": ("USER.md", result.user_updates),
            "memory": ("MEMORY.md", result.memory_updates),
        }

        for file_key, (file_name, updates_list) in file_mapping.items():
            if not _file_needs_update(analysis, file_key):
                continue

            try:
                if self._prompts is not None:
                    system_prompt = self._prompts.get_system(f"knowledge/{file_key}_update")
                    user_vars: dict[str, str] = {
                        f"current_{file_key}": existing_memories.get(file_name, ""),
                        "new_facts": analysis,
                        "memory_context": existing_memories.get("MEMORY.md", ""),
                    }
                    user_prompt = self._prompts.get_user(
                        f"knowledge/{file_key}_update",
                        **user_vars,
                    )
                    phase2_text = await self._summarizer.summarize(
                        user_prompt,
                        prompt=system_prompt,
                        max_tokens=2000,
                        temperature=0.2,
                    )
                else:
                    phase2_text = await self._summarizer.summarize(
                        f"## Analysis\n{analysis}\n\n{file_context}",
                        prompt=SummarizerAgent.PROMPT_MEMORY_UPDATE,
                        max_tokens=2000,
                        temperature=0.2,
                    )

                content = phase2_text.strip()
                if content.startswith("```"):
                    lines = content.splitlines()
                    if lines[0].startswith("```"):
                        lines = lines[1:]
                    if lines and lines[-1].startswith("```"):
                        lines = lines[:-1]
                    content = "\n".join(lines).strip()
                if content:
                    update = MemoryUpdate(
                        file_name=file_name,
                        content=content,
                        mode=str(MemoryUpdateMode.SECTION_REPLACE),
                        reason=f"DreamEngine per-file {file_key} update",
                    )
                    updates_list.append(update)
            except Exception as e:
                logger.warning("DreamEngine Phase 2 failed for %s: %s", file_key, e)

        return result

    _EMPTY_MARKERS = frozenset(
        {
            "(no conversation content)",
            "(no summary)",
            "(nothing)",
            "(no semantic content)",
        }
    )

    _TOOL_XML_PATTERNS = (
        "<minimax:tool_call>",
        "<tool_call>",
        "<function_call>",
        "<invoke name=",
    )

    @classmethod
    def _is_meaningful_entry(cls, entry: dict[str, Any]) -> bool:
        """Check whether an archive entry contains useful content for consolidation.

        Rejects empty summaries, known placeholder markers, entries
        that were explicitly marked as empty by the archive strategy
        (source=="empty" or semantic_count==0), and summaries that are
        raw tool-call XML leaked from LLM hallucination.
        """
        summary = entry.get("summary", "")
        if not summary or not summary.strip():
            return False
        stripped = summary.strip()
        if stripped in cls._EMPTY_MARKERS:
            return False
        if any(p in stripped for p in cls._TOOL_XML_PATTERNS):
            return False
        metadata = entry.get("metadata", {})
        if isinstance(metadata, dict):
            if metadata.get("source") == "empty":
                return False
            if metadata.get("semantic_count") == 0:
                return False
        return True

    @staticmethod
    def _archive_entry_to_dict(entry: ArchiveEntry) -> dict[str, Any]:
        metadata = dict(entry.metadata)
        return {
            "entry_id": entry.entry_id,
            "archive_id": entry.entry_id,
            "source_session_id": metadata.get("source_session_id"),
            "summary": entry.summary,
            "metadata": metadata,
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
                        mode=item.get("mode", str(MemoryUpdateMode.SECTION_REPLACE)),
                        reason=item.get("reason", ""),
                        search_text=item.get("search_text", ""),
                    )
                )
        return updates
