"""ExperienceReviewHook — AfterGraph hook that spawns the review agent.

The retired ``hook/builtin/experience_review.py``, moved into the
capability package (plan §10.3). Lifecycle change (plan §10.5): the hook
NO LONGER owns background tasks. Review submissions go through the
pool's ``ExperienceSupply``, which accepts them while running and
rejects them once stopping — so no review task can outlive supply
teardown (a hook-owned task set could, which was the §5.3 defect).

Features preserved verbatim:
- Triggers at "conversation segment completion" (plain assistant turn
  with stop_reason == "completed").
- Requires minimum accumulated messages before review eligibility.
- Sliding-window cooldown after experience tool usage: threshold is
  doubled for ``exp_cooldown_turns`` turns.
- Async mutex: only one review at a time (per supply).
- Post-review cleanup: validate, remove invalid, fix dir-name mismatches.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict

from modex_agent.core.emitter import StopReason
from modex_agent.hook.abc import AfterGraphHook
from modex_agent.memory.scope import MemoryContext
from modex_agent.memory.snapshot import (
    DEFAULT_SNAPSHOT_MAX_CONTENT_LEN,
    DEFAULT_SNAPSHOT_MAX_MESSAGES,
    format_snapshot_text,
)
from modex_agent.plugins.defaults.capabilities.experience.catalog import (
    ExperienceCatalog,
    auto_correct_frontmatter_name,
)
from modex_agent.plugins.defaults.capabilities.experience.paths import EXPERIENCE_FILENAME
from modex_agent.plugins.defaults.capabilities.experience.validation import (
    validate_experience_md,
)
from modex_agent.utils.xml import xml_attr, xml_text

if TYPE_CHECKING:
    from modex_agent.core.agent import AgentContext
    from modex_agent.core.emitter import AgentResult
    from modex_agent.core.message import ChatMessage
    from modex_agent.memory.core.system import MemorySystem
    from modex_agent.plugins.defaults.capabilities.experience.reviewer import (
        ExperienceReviewAgent,
    )
    from modex_agent.plugins.defaults.capabilities.experience.supply import ExperienceSupply

logger = logging.getLogger(__name__)


class _ReviewCursor(BaseModel):
    """Per-session cross-turn cooldown cursor."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    turn_count: int = 0
    last_exp_tool_turn: int = 0


class ExperienceReviewHook(AfterGraphHook):
    """AfterGraph hook that submits reviews to the pool's supply.

    Triggers only when the agent finishes a conversational exchange with a
    plain text response (``stop_reason == "completed"``). The hook keeps an
    independent cooldown cursor for each session because ``AgentContext`` does
    not track a cross-turn count.
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
        *,
        agent_name: str,
        supply: ExperienceSupply,
        memory_system: MemorySystem,
        catalog: ExperienceCatalog,
        min_messages: int = 10,
        exp_cooldown_turns: int = 3,
        snapshot_max_messages: int = DEFAULT_SNAPSHOT_MAX_MESSAGES,
        snapshot_max_content_len: int = DEFAULT_SNAPSHOT_MAX_CONTENT_LEN,
    ) -> None:
        self._agent_name = agent_name
        self._supply = supply
        self._memory_system = memory_system
        self._catalog = catalog
        self._min_messages = min_messages
        self._exp_cooldown_turns = exp_cooldown_turns
        self._snapshot_max_messages = snapshot_max_messages
        self._snapshot_max_content_len = snapshot_max_content_len
        self._review_cursors: dict[str, _ReviewCursor] = {}

    @property
    def name(self) -> str:
        return "experience_review_hook"

    # -- dir resolution --------------------------------------------------

    def _resolve_dir(self, ctx: AgentContext) -> Path:
        snap = ctx.workspace_snapshot
        if snap is not None and snap.experience_dir is not None:
            return snap.experience_dir
        return self._catalog.experience_dir

    # -- hook lifecycle --------------------------------------------------

    async def after_graph(
        self,
        ctx: AgentContext,
        result: AgentResult,
    ) -> None:
        session_id = ctx.session.session_id
        previous = self._review_cursors.get(session_id, _ReviewCursor())
        cursor = previous.model_copy(update={"turn_count": previous.turn_count + 1})
        self._review_cursors[session_id] = cursor
        turn_count = cursor.turn_count

        # Get history via async to_list() — MessageHistory has no __len__ / __iter__
        try:
            history_list = await ctx.history.to_list()
        except Exception:
            logger.info(
                "ExperienceReviewHook: skipped (history_to_list_error) turn=%s",
                turn_count,
            )
            return
        history_len = len(history_list)

        # Gate 0: async mutex — one review at a time, per supply
        if self._supply.review_in_flight(self._agent_name):
            logger.info(
                "ExperienceReviewHook: skipped (mutex) — review already in progress turn=%s",
                turn_count,
            )
            return

        tool_names = self._extract_tool_names(result)
        skip_reason = self._should_review(
            result,
            tool_names,
            history_len,
            session_id=session_id,
            cursor=cursor,
        )
        if skip_reason:
            logger.info(
                "ExperienceReviewHook: skipped (%s) turn=%s",
                skip_reason,
                turn_count,
            )
            return

        snapshot = self._capture_snapshot(history_list)
        if not snapshot:
            logger.info(
                "ExperienceReviewHook: skipped (empty snapshot) turn=%s",
                turn_count,
            )
            return

        # Resolve the experience dir for THIS turn from the per-turn
        # workspace snapshot (or the catalog's root).
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
                turn_count,
            )
            return

        invocation_id = uuid.uuid4().hex
        logger.info(
            "ExperienceReviewHook: triggering review invocation=%s turn=%s dir=%s fork=%s",
            invocation_id,
            turn_count,
            exp_dir,
            bool(fork_messages),
        )

        # Lifecycle: submit through the supply — it owns the task, accepts
        # while running, and rejects during stop (no orphanable hook task).
        self._supply.submit_review(
            agent_name=self._agent_name,
            review_factory=lambda: self._do_review(
                snapshot,
                existing_xml,
                invocation_id,
                exp_dir,
                fork_messages,
            ),
            invocation_id=invocation_id,
        )

    # -- internal --------------------------------------------------------

    async def _build_existing_experiences_xml(self, exp_dir: Path) -> str:
        """Gather existing experience summaries as XML for the reviewer.

        Includes name, description, tags, scenario, and directory so the
        reviewer can inspect an experience's sub-files before deciding
        whether to update or extend it.
        """
        try:
            summaries = await self._catalog.list_summaries()
            if not summaries:
                return ""
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
        review_agent: ExperienceReviewAgent | None = self._supply.review_agent_for(
            self._agent_name
        )
        if review_agent is None:
            # Fail-soft missing review LLM (§10.6): skip with a warning —
            # tool, section, storage, and curator remain available.
            logger.warning(
                "ExperienceReviewHook: no review provider for agent %r — "
                "review skipped (invocation=%s)",
                self._agent_name,
                invocation_id,
            )
            return

        before = self._scan_experience_dir(exp_dir)
        try:
            ok = await review_agent.review(
                conversation_snapshot=snapshot,
                experience_dir=exp_dir,
                meta_store=self._catalog.meta_store,
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
            md = entry / EXPERIENCE_FILENAME
            if md.exists():
                with contextlib.suppress(OSError):
                    result[entry.name] = md.stat().st_mtime
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

        meta_store = self._catalog.meta_store

        # Remove records for deleted experiences
        for name in deleted:
            meta_store.remove(name)

        # Refresh timestamps for created/modified
        for name in created | modified:
            mtime = after[name]
            record = meta_store.get(name)
            if record is not None:
                dt = datetime.fromtimestamp(mtime, tz=UTC)
                record = record.with_last_used(dt.isoformat())
                meta_store.set(name, record)

        # Validate all existing EXPERIENCE.md files
        for name in list(after.keys()):
            md_path = exp_dir / name / EXPERIENCE_FILENAME
            if not md_path.exists():
                continue
            try:
                text = md_path.read_text(encoding="utf-8")
            except OSError:
                continue

            result = validate_experience_md(text, dir_name=name)
            if not result.valid:
                with contextlib.suppress(OSError):
                    md_path.unlink()
                dir_path = exp_dir / name
                try:
                    if dir_path.exists() and not any(dir_path.iterdir()):
                        dir_path.rmdir()
                except OSError:
                    pass
                meta_store.remove(name)
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
        *,
        session_id: str,
        cursor: _ReviewCursor,
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
        Gate 4: Non-empty snapshot (checked separately in after_graph).
        """
        # Gate 1: Plain completion
        if result.stop_reason != StopReason.COMPLETED:
            return f"stop_reason={result.stop_reason}, need=completed"

        # Gate 2: Experience tool usage this turn → cooldown
        if self._detect_exp_edit(tool_names, result):
            cursor = cursor.model_copy(
                update={"last_exp_tool_turn": cursor.turn_count}
            )
            self._review_cursors[session_id] = cursor
            logger.info(
                "ExperienceReviewHook: exp write/edit detected, cooldown started turn=%s",
                cursor.turn_count,
            )
            return f"exp_edit_detected cooldown_start_turn={cursor.turn_count}"

        # Gate 3: Sufficient conversation length
        effective_threshold = self._min_messages
        if cursor.last_exp_tool_turn > 0:
            turns_since = cursor.turn_count - cursor.last_exp_tool_turn
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
            list(messages),
            max_messages=self._snapshot_max_messages,
            max_content_len=self._snapshot_max_content_len,
        )


__all__ = ["ExperienceReviewHook"]
