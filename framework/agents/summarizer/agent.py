"""SummarizerAgent — lightweight single-turn agent for text summarization.

A dedicated, tool-free agent designed for mid/long-term memory generation.
Makes one LLM call per invocation — no tool loop, no iteration.
"""

from __future__ import annotations

import logging
from enum import Enum
from typing import Any

from framework.core.agent import Agent, AgentContext, AgentResult
from framework.core.constants import FinishReason
from framework.core.emitter import ContentEmitter
from framework.core.events import AgentEvent
from framework.core.provider import LLMProvider
from framework.core.types import LLMResponse

logger = logging.getLogger(__name__)


class SummarizerEvent(AgentEvent, Enum):
    """Events emitted by SummarizerAgent."""

    START = "start"
    CONTENT = "content"
    COMPLETE = "complete"
    ERROR = "error"


class SummarizerAgent(Agent[SummarizerEvent]):
    """Lightweight single-turn agent for text summarization and analysis.

    No tools, no iteration loop. Makes one LLM call and returns the result.
    Designed as a reusable building block for mid/long-term memory generation.
    """

    event_enum = SummarizerEvent

    PROMPT_COMPRESSION = """You are a conversation archiver creating a medium-term memory record.

Task: Summarize the provided conversation messages into a structured archive entry that a DreamEngine will later use to extract long-term knowledge.

Output format (plain text, no markdown):

[Task] The user's main request or goal in one sentence.
[Actions] Numbered list of concrete actions and tools used with their results.
[Decisions] Any important decisions made and WHY.
[State] Current working context (files, environment, errors, etc.).
[Pending] Anything still in progress or unanswered — write "None" if nothing.

Rules:
1. Be CONCRETE — include file paths, values, error messages, tool names
2. Skip trivial pleasantries, greetings, and redundant repetition
3. Output 300-800 characters — enough detail for future retrieval
4. Plain text only, no markdown formatting, no JSON
5. Do NOT include any thinking/reasoning tags in output
6. Write in the same language the conversation was in

If the conversation contains no meaningful content, output exactly: (nothing)"""

    PROMPT_FACT_EXTRACTION = """You are a memory analysis assistant.

Task: Analyze the conversation summaries below and extract facts worth remembering.

Long-term memory files:
- SOUL.md: bot behavior, tone, personality, communication style
- USER.md: user identity, preferences, habits, recurring patterns
- MEMORY.md: knowledge, project context, decisions, solutions

Output one line per finding using this format:
[FILE] atomic fact description

Where FILE is one of: SOUL, USER, MEMORY

Priority: user corrections > solutions > decisions > user preferences > events > environment facts

Rules:
- Only NEW or CONFLICTING information — skip anything already in current files
- Use atomic facts: "prefers dark mode" not "discussed theme settings"
- Corrections: [USER] location is Tokyo, not Osaka
- Skip: code patterns, git history, tool invocation details, anything already in current files
- Skip: trivial pleasantries, greetings, acknowledgments
- Keep output concise — under 500 tokens
- Do NOT include any thinking/reasoning tags in output
- If nothing noteworthy: [SKIP] no new information"""

    PROMPT_MEMORY_UPDATE = """You are a memory editing assistant.

Task: Based on the analysis below, produce a JSON array of update instructions for the long-term memory files.

Each update must be a JSON object with:
- "file_name": one of "SOUL.md", "USER.md", "MEMORY.md"
- "mode": one of "incremental", "append", "section_replace", "replace_text"
- "content": the new or updated content to write
- "reason": brief explanation of why this update is needed
- "search_text": (only for "replace_text" mode) the exact existing text to find and replace

Rules:
1. Use "replace_text" when modifying existing information (most precise)
2. Use "append" for adding new facts at the end
3. Use "section_replace" only when rewriting a whole section
4. Use "incremental" for small additions when no exact text can be matched
5. Do not duplicate existing content
6. Keep total output under 1000 tokens — be concise, use atomic facts
7. Return ONLY a valid JSON array. No markdown code blocks, no extra text.
8. Do NOT include any thinking/reasoning tags in output

Example output:
[
  {"file_name": "MEMORY.md", "mode": "append", "content": "- User prefers dark mode\\n", "reason": "new preference"},
  {"file_name": "USER.md", "mode": "replace_text", "search_text": "- Location: Tokyo\\n", "content": "- Location: Osaka\\n", "reason": "location corrected"}
]"""

    PROMPT_KNOWLEDGE_CONSOLIDATION = """You are a knowledge consolidation assistant.

Task: The knowledge file below has grown too large. Consolidate it into a compact version.

Rules:
1. Preserve ALL important facts — do not lose unique information
2. Remove redundancy: merge duplicate or overlapping facts into one
3. Remove outdated/superseded information (newer facts override older ones)
4. Organize by topic with ## headers
5. Use concise bullet points (- prefix)
6. Output must be under 1500 tokens
7. Output plain markdown only — no JSON, no thinking tags
8. If the content is already concise, output it as-is"""

    def __init__(self, provider: LLMProvider) -> None:
        self.provider = provider

    @property
    def name(self) -> str:
        return "summarizer"

    # -- Agent interface -------------------------------------------------------

    async def run(
        self,
        context: AgentContext,
        emitter: ContentEmitter[SummarizerEvent],
    ) -> AgentResult:
        """Standard Agent.run() — single LLM call, no tools."""
        messages = await context.to_messages()
        if not messages:
            return AgentResult(content="", stop_reason="completed")

        try:
            await emitter.emit(SummarizerEvent.START)
            response = await self._call_llm(messages)

            content = self._extract_content(response)
            if content:
                await emitter.emit(SummarizerEvent.CONTENT, content)
            await emitter.emit(SummarizerEvent.COMPLETE)

            return AgentResult(content=content, stop_reason="completed")
        except Exception as e:
            logger.warning("SummarizerAgent.run failed: %s", e)
            await emitter.emit(SummarizerEvent.ERROR, str(e))
            return AgentResult(error=str(e), stop_reason="error")

    # -- Convenience API -------------------------------------------------------

    async def summarize(
        self,
        text: str,
        *,
        prompt: str | None = None,
        max_tokens: int = 500,
        temperature: float = 0.3,
    ) -> str:
        """Summarize text with a given prompt. Returns empty string on failure."""
        system_prompt = prompt or self.PROMPT_COMPRESSION
        try:
            response = await self._call_llm(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": text},
                ],
                max_tokens=max_tokens,
                temperature=temperature,
            )
            content = self._extract_content(response)
            return content.strip() if content else ""
        except Exception as e:
            logger.warning("SummarizerAgent.summarize failed: %s", e)
            return ""

    async def analyze(
        self,
        content: str,
        *,
        prompt: str | None = None,
        max_tokens: int = 2000,
        temperature: float = 0.3,
    ) -> str:
        """Analyze content and extract structured information.

        Defaults to PROMPT_FACT_EXTRACTION and higher token limit.
        """
        system_prompt = prompt or self.PROMPT_FACT_EXTRACTION
        try:
            response = await self._call_llm(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": content},
                ],
                max_tokens=max_tokens,
                temperature=temperature,
            )
            result = self._extract_content(response)
            return result.strip() if result else ""
        except Exception as e:
            logger.warning("SummarizerAgent.analyze failed: %s", e)
            return ""

    # -- Internal helpers -------------------------------------------------------

    async def _call_llm(
        self,
        messages: list[dict[str, Any]],
        *,
        max_tokens: int = 500,
        temperature: float = 0.3,
    ) -> str | LLMResponse:
        chat_fn = getattr(self.provider, "chat_with_retry", self.provider.chat)
        return await chat_fn(
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
        )

    @staticmethod
    def _extract_content(response: Any) -> str:
        from framework.utils.helpers import strip_think

        if isinstance(response, LLMResponse):
            if response.finish_reason == FinishReason.ERROR.value:
                logger.warning(
                    "SummarizerAgent received error response: %s",
                    (response.error or "")[:200],
                )
                return ""
            raw = response.content or ""
        elif isinstance(response, str):
            raw = response.strip()
        else:
            raw = str(response).strip()
        cleaned = strip_think(raw)
        return cleaned or ""
