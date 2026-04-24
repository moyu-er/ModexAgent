"""DreamEngine — two-phase offline memory consolidation."""

import json
import logging
import re
from typing import Any

from framework.core.provider import LLMProvider
from framework.memory.core.consolidation import (
    ConsolidationEngine,
    ConsolidationResult,
    MemoryUpdate,
)
from framework.memory.core.message import ChatMessage
from framework.memory.core.scope import (
    MemoryAgentRole,
    MemoryContext,
    MemoryLayerName,
)
from framework.memory.core.storage import MemoryStorage
from framework.memory.managers.history import HistoryArchiveManager
from framework.memory.managers.long_term import LongTermMemoryManager

logger = logging.getLogger(__name__)

_PHASE1_PROMPT = """You are a memory analysis assistant.

Task: Analyze the following conversation history summaries and identify what needs to be updated in the agent's long-term memory files.

Long-term memory files:
- SOUL.md: bot behavior, tone, personality
- USER.md: user identity, preferences, habits
- MEMORY.md: knowledge, project context, tool patterns

Output one line per finding using this format:
[FILE] atomic fact or change description

Where FILE is one of: SOUL, USER, MEMORY

Rules:
- Only new or conflicting information — skip duplicates
- Prefer atomic facts: "has a cat named Luna" not "discussed pet care"
- Corrections: [USER] location is Tokyo, not Osaka
- If nothing needs updating: [SKIP] no new information
"""

_PHASE2_PROMPT = """You are a memory editing assistant.

Task: Based on the analysis below, produce a JSON array of update instructions for the long-term memory files.

Each update must be a JSON object with:
- "file_name": one of "SOUL.md", "USER.md", "MEMORY.md"
- "mode": one of "incremental", "append", "section_replace", "replace_text"
- "content": the new or updated content to write
- "reason": brief explanation of why this update is needed
- "search_text": (only for "replace_text" mode) the exact existing text to find and replace

Rules:
1. Use "replace_text" when modifying existing information (most precise)
   - Provide the exact "search_text" found in the current file
   - "content" becomes the replacement text
2. Use "append" for adding new facts at the end
3. Use "section_replace" only when rewriting a whole section
4. Use "incremental" for small additions when no exact text can be matched
5. Do not duplicate existing content
6. Return ONLY a valid JSON array. No markdown code blocks, no extra text.

Example output:
[
  {"file_name": "MEMORY.md", "mode": "append", "content": "- User prefers dark mode\\n", "reason": "new preference"},
  {"file_name": "USER.md", "mode": "replace_text", "search_text": "- Location: Tokyo\\n", "content": "- Location: Osaka\\n", "reason": "location corrected"}
]
"""


def _msg_to_dict(msg: ChatMessage | dict[str, Any]) -> dict[str, Any]:
    """将 ChatMessage 或 dict 统一转为 dict。"""
    return msg.to_dict() if isinstance(msg, ChatMessage) else msg


class DreamEngine(ConsolidationEngine):
    """离线 DreamEngine：两阶段长期记忆整合。

    Phase 1: 使用 LLM 分析未处理的历史摘要
    Phase 2: 使用 LLM 生成具体的 MemoryUpdate 指令并应用到长期记忆

    特点：
    - cursor 始终前进，防止无限重试
    - LLM 失败时返回空更新，不阻塞后续处理
    """

    def __init__(
        self,
        llm_provider: LLMProvider,
        history_manager: HistoryArchiveManager,
        long_term_manager: LongTermMemoryManager,
        max_batch_size: int = 20,
        max_iterations: int = 10,
        storage: MemoryStorage | None = None,
    ):
        self.llm = llm_provider
        self.history_manager = history_manager
        self.long_term_manager = long_term_manager
        self.max_batch_size = max_batch_size
        self.max_iterations = max_iterations
        self.storage = storage

    async def run(self, context: MemoryContext) -> bool:
        """处理未处理的历史条目。

        Returns:
            如果实际处理了条目则返回 True，否则返回 False
        """
        new_cursor, entries = await self.history_manager.get_unprocessed(
            context, cursor_name="dream"
        )
        if not entries:
            return False

        batch = entries[: self.max_batch_size]
        logger.debug(
            "DreamEngine: processing %s entries (cursor %s → %s)",
            len(batch),
            new_cursor - len(batch),  # approximate
            batch[-1].get("cursor", new_cursor),
        )

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
            new_entries=batch,
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

        # Cursor always advances
        final_cursor = max(e.get("cursor", 0) for e in batch)
        await self.history_manager.commit_cursor(context, "dream", final_cursor)
        logger.debug("DreamEngine cursor advanced to %s", final_cursor)

        return True

    async def scan_all(self) -> list[MemoryContext]:
        """扫描 history 层 scope records，返回处理过的 MemoryContext 列表。

        只处理 main agent 的 scope；peer/subagent 被过滤掉。
        每个 scope 调用 run() 处理未处理的 history 条目。
        """
        processed: list[MemoryContext] = []
        if self.storage is None:
            logger.warning("DreamEngine.scan_all skipped: no storage configured")
            return processed

        records = await self.storage.list_scope_records(
            layer=MemoryLayerName.HISTORY,
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
        new_entries: list[ChatMessage | dict[str, Any]],
        existing_memories: dict[str, str],
    ) -> ConsolidationResult:
        """整合新历史条目到长期记忆。"""
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

        # Phase 1: Analysis
        try:
            phase1_response = await self.llm.chat_with_retry(
                messages=[
                    {"role": "system", "content": _PHASE1_PROMPT},
                    {
                        "role": "user",
                        "content": f"## History\n{history_text}\n\n{file_context}",
                    },
                ],
                temperature=0.3,
                max_tokens=2000,
            )
            analysis = (
                phase1_response.strip()
                if isinstance(phase1_response, str)
                else str(phase1_response).strip()
            )
        except Exception as e:
            logger.warning("DreamEngine Phase 1 failed: %s", e)
            return ConsolidationResult(success=False, reasoning=f"Phase 1 error: {e}")

        if "[SKIP]" in analysis:
            return ConsolidationResult(
                success=True,
                reasoning="Phase 1: no new information",
            )

        # Phase 2: Generate updates
        try:
            phase2_response = await self.llm.chat_with_retry(
                messages=[
                    {"role": "system", "content": _PHASE2_PROMPT},
                    {
                        "role": "user",
                        "content": f"## Analysis\n{analysis}\n\n{file_context}",
                    },
                ],
                temperature=0.2,
                max_tokens=2000,
            )
            updates = self._parse_updates(phase2_response)
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

