"""ExperienceReviewHook — AfterTurn hook that spawns an ExperienceReviewAgent.

Features:
- Triggers at "conversation segment completion" (plain assistant turn with
  stop_reason == "completed").
- Requires minimum accumulated messages before review eligibility.
- Sliding-window cooldown after experience tool usage: threshold is doubled
  for ``exp_cooldown_turns`` turns.
- Async mutex: only one review at a time.
- Post-review cleanup: validate, remove invalid, fix dir-name mismatches.
- Pending task tracking with cancel_pending() for workspace switch.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from modex_agent.agents.experience.review_agent import ExperienceReviewAgent
from modex_agent.core.agent import AgentContext
from modex_agent.core.constants import StopReason
from modex_agent.core.emitter import AgentResult
from modex_agent.core.experience import (
    ExperienceMetaStore,
    FileExperienceSource,
    auto_correct_frontmatter_name,
    validate_experience_md,
)
from modex_agent.core.message import ChatMessage
from modex_agent.core.scope import MemoryContext
from modex_agent.hook.abc import AfterTurnHook
from modex_agent.memory.core.system import MemorySystem
from modex_agent.memory.snapshot import (
    DEFAULT_SNAPSHOT_MAX_CONTENT_LEN,
    DEFAULT_SNAPSHOT_MAX_MESSAGES,
    format_snapshot_text,
)

logger = logging.getLogger(__name__)


class ExperienceReviewHook(AfterTurnHook):
    """AfterTurn hook that spawns an ExperienceReviewAgent.

    Triggers only when the agent finishes a conversational exchange with a
    plain text response (``stop_reason == "completed"``).  The hook keeps
    an internal turn counter because ``AgentContext`` does not track turn
    count.
    """

    # Tool names that indicate the agent is already managing experiences
    _EXP_EDIT_TOOL_NAMES = frozenset(
        {
            "experience_write",
            "experience_edit",
        }
    )

    def __init__(
        self,
        review_agent: ExperienceReviewAgent,
        memory_system: MemorySystem,
        experience_dir: Path | Callable[[], Path],
        meta_store: ExperienceMetaStore,
        min_messages: int = 10,
        exp_cooldown_turns: int = 3,
        snapshot_max_messages: int = DEFAULT_SNAPSHOT_MAX_MESSAGES,
        snapshot_max_content_len: int = DEFAULT_SNAPSHOT_MAX_CONTENT_LEN,
    ) -> None:
        self._agent = review_agent
        self._memory_system = memory_system
        self._get_dir: Callable[[], Path] = (
            experience_dir if callable(experience_dir) else lambda: experience_dir
        )
        self._meta_store = meta_store
        self._min_messages = min_messages
        self._exp_cooldown_turns = exp_cooldown_turns
        self._snapshot_max_messages = snapshot_max_messages
        self._snapshot_max_content_len = snapshot_max_content_len
        self._pending: set[asyncio.Task[None]] = set()
        # Internal turn counter (AgentContext has no turn_count field)
        self._turn_counter: int = 0
        # Turn number when experience tool was last used (0 = never)
        self._last_exp_tool_turn: int = 0

    @property
    def name(self) -> str:
        return "experience_review_hook"

    # -- dir resolution --------------------------------------------------

    def _resolve_dir(self, ctx: AgentContext) -> Path:
        snap = ctx.workspace_snapshot
        if snap is not None and snap.experience_dir is not None:
            return snap.experience_dir
        return self._get_dir()

    # -- hook lifecycle --------------------------------------------------

    async def after_turn(
        self,
        ctx: AgentContext,
        result: AgentResult,
    ) -> None:
        self._turn_counter += 1

        # Get history via async to_list() — MessageHistory has no __len__ / __iter__
        try:
            history_list = await ctx.history.to_list()
        except Exception:
            logger.info(
                "ExperienceReviewHook: skipped (history_to_list_error) turn=%s",
                self._turn_counter,
            )
            return
        history_len = len(history_list)

        logger.info(
            "ExperienceReviewHook: ENTERED turn=%s stop_reason=%s history_len=%s pending=%s",
            self._turn_counter,
            result.stop_reason,
            history_len,
            bool(self._pending),
        )

        # Gate 0: async mutex
        if self._pending:
            logger.info(
                "ExperienceReviewHook: skipped (mutex) — review already in progress turn=%s",
                self._turn_counter,
            )
            return

        tool_names = self._extract_tool_names(result)
        skip_reason = self._should_review(result, tool_names, history_len)
        if skip_reason:
            logger.info(
                "ExperienceReviewHook: skipped (%s) turn=%s",
                skip_reason,
                self._turn_counter,
            )
            return

        snapshot = self._capture_snapshot(history_list)
        if not snapshot:
            logger.info(
                "ExperienceReviewHook: skipped (empty snapshot) turn=%s",
                self._turn_counter,
            )
            return

        # Resolve the experience dir for THIS turn from the per-turn
        # workspace snapshot (or the hook's configured fallback).
        exp_dir = self._resolve_dir(ctx)

        # Gather existing experiences for user message
        existing_xml = await self._build_existing_experiences_xml(exp_dir)

        context = MemoryContext(session_id=str(ctx.session))
        fork_messages = await self._memory_system.get_full_history(
            context,
            limit=self._snapshot_max_messages,
        )
        if not fork_messages:
            logger.info(
                "ExperienceReviewHook: no messages to review, skipping turn=%s",
                self._turn_counter,
            )
            return

        invocation_id = uuid.uuid4().hex
        logger.info(
            "ExperienceReviewHook: triggering review invocation=%s turn=%s dir=%s fork=%s",
            invocation_id,
            self._turn_counter,
            exp_dir,
            bool(fork_messages),
        )

        task = asyncio.create_task(
            self._do_review(snapshot, existing_xml, invocation_id, exp_dir, fork_messages),
            name=f"exp-review-{invocation_id[:8]}",
        )
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    async def cancel_pending(self) -> None:
        """Cancel all in-flight review tasks (workspace switch / shutdown)."""
        for task in list(self._pending):
            task.cancel()
        if self._pending:
            await asyncio.gather(*list(self._pending), return_exceptions=True)

    # -- internal --------------------------------------------------------

    async def _build_existing_experiences_xml(self, exp_dir: Path) -> str:
        """Gather existing experience summaries as XML for the reviewer.

        Includes name, description, tags, scenario, and directory so the
        reviewer can inspect an experience's sub-files before deciding
        whether to update or extend it.
        """
        try:
            source = FileExperienceSource(directories=[exp_dir])
            summaries = await source.list_experiences()
            if not summaries:
                return ""
            from modex_agent.utils.xml import xml_attr, xml_text

            entries: list[str] = []
            for s in summaries:
                parts = [
                    f'  <experience name="{xml_attr(s.name)}"',
                ]
                if s.directory:
                    parts.append(f' directory="{xml_attr(s.directory)}"')
                if s.tags:
                    parts.append(f' tags="{xml_attr(",".join(s.tags))}"')
                if s.scenario:
                    parts.append(f' scenario="{xml_attr(s.scenario)}"')
                parts.append(">")
                entries.append("".join(parts))
                if s.description:
                    entries.append(f"    <description>{xml_text(s.description)}</description>")
                entries.append("  </experience>")
            return "<experiences>\n" + "\n".join(entries) + "\n</experiences>"
        except Exception:
            logger.debug("Failed to build existing experiences XML", exc_info=True)
            return ""

    async def _do_review(
        self,
        snapshot: str,
        existing_xml: str,
        invocation_id: str,
        exp_dir: Path,
        fork_messages: list[ChatMessage],
    ) -> None:
        before = self._scan_experience_dir(exp_dir)
        try:
            ok = await self._agent.review(
                conversation_snapshot=snapshot,
                experience_dir=exp_dir,
                meta_store=self._meta_store,
                existing_experiences=existing_xml,
                invocation_id=invocation_id,
                conversation_messages=fork_messages,
            )
            logger.info(
                "ExperienceReviewHook: review completed invocation=%s ok=%s",
                invocation_id,
                ok,
            )
        except asyncio.CancelledError:
            logger.info(
                "ExperienceReviewHook: review cancelled invocation=%s",
                invocation_id,
            )
        except Exception:
            logger.exception(
                "ExperienceReviewHook: review failed invocation=%s",
                invocation_id,
            )
        finally:
            after = self._scan_experience_dir(exp_dir)
            await self._cleanup(before, after, exp_dir)

    def _scan_experience_dir(self, exp_dir: Path) -> dict[str, float]:
        """Scan experience directory, return {name: mtime}."""
        result: dict[str, float] = {}
        if not exp_dir.exists():
            return result
        for entry in exp_dir.iterdir():
            if not entry.is_dir() or entry.name.startswith("."):
                continue
            md = entry / "EXPERIENCE.md"
            if md.exists():
                try:
                    result[entry.name] = md.stat().st_mtime
                except OSError:
                    pass
        return result

    async def _cleanup(
        self,
        before: dict[str, float],
        after: dict[str, float],
        exp_dir: Path,
    ) -> None:
        """Deterministic post-review cleanup."""
        created = after.keys() - before.keys()
        modified = {k for k in after if before.get(k) != after.get(k)}
        deleted = before.keys() - after.keys()

        # Remove records for deleted experiences
        for name in deleted:
            self._meta_store.remove(name)

        # Refresh timestamps for created/modified
        for name in created | modified:
            mtime = after[name]
            record = self._meta_store.get(name)
            if record is not None:
                dt = datetime.fromtimestamp(mtime, tz=UTC)
                record.last_used_at = dt.isoformat()
                if record.created_at is None:
                    record.created_at = dt.isoformat()
                self._meta_store.set(name, record)

        # Validate all existing EXPERIENCE.md files
        for name in list(after.keys()):
            md_path = exp_dir / name / "EXPERIENCE.md"
            if not md_path.exists():
                continue
            try:
                text = md_path.read_text(encoding="utf-8")
            except OSError:
                continue

            result = validate_experience_md(text, dir_name=name)
            if not result.valid:
                try:
                    md_path.unlink()
                except OSError:
                    pass
                dir_path = exp_dir / name
                try:
                    if dir_path.exists() and not any(dir_path.iterdir()):
                        dir_path.rmdir()
                except OSError:
                    pass
                self._meta_store.remove(name)
                logger.warning("Removed invalid experience: %s (%s)", name, result.errors)
                continue

            # Auto-correct frontmatter name to match directory name
            auto_correct_frontmatter_name(exp_dir / name)

    # -- trigger logic ---------------------------------------------------

    def _should_review(
        self,
        result: AgentResult,
        tool_names: set[str],
        msg_count: int,
    ) -> str:
        """Gate check before triggering review.

        Returns empty string if all gates pass, or a skip reason string.

        Gate 1: Turn completed with a plain assistant response
                (stop_reason == "completed").
        Gate 2: Detect experience write/edit usage this turn.
                Sets cooldown and returns False so the review doesn't
                run immediately after the agent edits experiences.
        Gate 3: Sufficient conversation history length (>= threshold).
                During cooldown the effective threshold is doubled.
        Gate 4: Non-empty snapshot (checked separately in after_turn).
        """
        # Gate 1: Plain completion
        if result.stop_reason != StopReason.COMPLETED:
            return f"stop_reason={result.stop_reason}, need=completed"

        # Gate 2: Experience tool usage this turn → cooldown
        if self._detect_exp_edit(tool_names, result):
            self._last_exp_tool_turn = self._turn_counter
            logger.info(
                "ExperienceReviewHook: exp write/edit detected, cooldown started turn=%s",
                self._turn_counter,
            )
            return f"exp_edit_detected cooldown_start_turn={self._turn_counter}"

        # Gate 3: Sufficient conversation length
        effective_threshold = self._min_messages
        if self._last_exp_tool_turn > 0:
            turns_since = self._turn_counter - self._last_exp_tool_turn
            if turns_since <= self._exp_cooldown_turns:
                effective_threshold = self._min_messages * 2
                if msg_count < effective_threshold:
                    return (
                        f"cooldown msg_count={msg_count} < {effective_threshold} "
                        f"(turns_since={turns_since}/{self._exp_cooldown_turns})"
                    )

        if msg_count < effective_threshold:
            return f"msg_count={msg_count} < {effective_threshold}"

        return ""

    def _extract_tool_names(self, result: AgentResult) -> set[str]:
        """Extract tool names from this turn's result messages.

        Tool calls in result.messages follow the OpenAI format:
        {"function": {"name": "tool_name", "arguments": "..."}}.
        """
        names: set[str] = set()
        for msg in result.messages:
            if isinstance(msg, dict):
                tc_list = msg.get("tool_calls")
            else:
                tc_list = getattr(msg, "tool_calls", None)
            if not tc_list:
                continue
            for tc in tc_list:
                if isinstance(tc, dict):
                    func = tc.get("function", {})
                    name = func.get("name", "") if isinstance(func, dict) else ""
                else:
                    name = getattr(tc, "tool_name", "") or getattr(tc, "name", "")
                if name:
                    names.add(name)
        return names

    def _detect_exp_edit(
        self,
        tool_names: set[str],
        result: AgentResult,
    ) -> bool:
        """Check for experience write/edit tool usage.

        Handles three forms:
        - experience_write / experience_edit (explicit tool names)
        - experience tool with action="write" or action="edit"
        """
        if tool_names & self._EXP_EDIT_TOOL_NAMES:
            return True

        if "experience" not in tool_names:
            return False

        # Look for experience tool with write/edit action in arguments
        for msg in result.messages:
            if isinstance(msg, dict):
                tc_list = msg.get("tool_calls")
            else:
                tc_list = getattr(msg, "tool_calls", None)
            if not tc_list or not isinstance(tc_list, list):
                continue
            for tc in tc_list:
                if not isinstance(tc, dict):
                    continue
                func = tc.get("function", {})
                if not isinstance(func, dict):
                    continue
                if func.get("name") != "experience":
                    continue
                args_str = func.get("arguments", "")
                try:
                    args = json.loads(args_str) if isinstance(args_str, str) else args_str
                    if isinstance(args, dict) and args.get("action") in ("write", "edit"):
                        return True
                except (json.JSONDecodeError, TypeError):
                    pass

        return False

    def _capture_snapshot(self, messages: Sequence[Any]) -> str:
        """Extract recent user/assistant messages as a text snapshot."""
        return format_snapshot_text(
            messages,
            max_messages=self._snapshot_max_messages,
            max_content_len=self._snapshot_max_content_len,
        )
