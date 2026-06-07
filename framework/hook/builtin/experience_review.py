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
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from framework.core.agent import AgentContext
from framework.core.emitter import AgentResult
from framework.hook.abc import AfterTurnHook

from framework.agents.experience.review_agent import ExperienceReviewAgent
from framework.core.experience.meta import ExperienceMetaStore
from framework.core.experience.name_sync import auto_correct_frontmatter_name
from framework.core.experience.source import FileExperienceSource
from framework.core.experience.validation import validate_experience_md

logger = logging.getLogger(__name__)

_SNAPSHOT_MAX_MESSAGES = 20
_SNAPSHOT_MAX_CONTENT_LEN = 1200


class ExperienceReviewHook(AfterTurnHook):
    """AfterTurn hook that spawns an ExperienceReviewAgent.

    Triggers only when the agent finishes a conversational exchange with a
    plain text response (``stop_reason == "completed"``).  The hook keeps
    an internal turn counter because ``AgentContext`` does not track turn
    count.
    """

    # Tool names that indicate the agent is already managing experiences
    _EXP_EDIT_TOOL_NAMES = frozenset({
        "experience_write",
        "experience_edit",
    })

    def __init__(
        self,
        review_agent: ExperienceReviewAgent,
        experience_dir: Path | Callable[[], Path],
        meta_store: ExperienceMetaStore,
        min_messages: int = 6,
        exp_cooldown_turns: int = 3,
    ) -> None:
        self._agent = review_agent
        self._get_dir = experience_dir if callable(experience_dir) else lambda: experience_dir
        self._meta_store = meta_store
        self._min_messages = min_messages
        self._exp_cooldown_turns = exp_cooldown_turns
        self._pending: set[asyncio.Task] = set()
        # Internal turn counter (AgentContext has no turn_count field)
        self._turn_counter: int = 0
        # Turn number when experience tool was last used (0 = never)
        self._last_exp_tool_turn: int = 0

    @property
    def name(self) -> str:
        return "experience_review_hook"

    # -- hook lifecycle --------------------------------------------------

    async def after_turn(
        self,
        ctx: AgentContext,
        result: AgentResult,
    ) -> None:
        self._turn_counter += 1

        # Gate 0: async mutex
        if self._pending:
            logger.debug(
                "ExperienceReviewHook: skipped — review already in progress"
            )
            return

        tool_names = self._extract_tool_names(result)
        if not self._should_review(ctx, result, tool_names):
            logger.debug(
                "ExperienceReviewHook: skipped turn=%s min_messages=%s",
                self._turn_counter, self._min_messages,
            )
            return

        snapshot = self._capture_snapshot(ctx)
        if not snapshot:
            logger.debug(
                "ExperienceReviewHook: skipped (empty snapshot) turn=%s",
                self._turn_counter,
            )
            return

        # Gather existing experiences for user message
        existing_xml = await self._build_existing_experiences_xml()

        invocation_id = uuid.uuid4().hex
        logger.info(
            "ExperienceReviewHook: triggering review invocation=%s turn=%s",
            invocation_id, self._turn_counter,
        )

        task = asyncio.create_task(
            self._do_review(snapshot, existing_xml, invocation_id),
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

    async def _build_existing_experiences_xml(self) -> str:
        """Gather existing experience summaries as XML for the reviewer.

        Includes name, description, tags, scenario, and directory so the
        reviewer can inspect an experience's sub-files before deciding
        whether to update or extend it.
        """
        try:
            source = FileExperienceSource(directories=[self._get_dir()])
            summaries = await source.list_experiences()
            if not summaries:
                return ""
            from xml.sax.saxutils import escape as _esc

            def _attr(v: str) -> str:
                return _esc(v).replace('"', "&quot;")

            entries: list[str] = []
            for s in summaries:
                parts = [
                    f'  <experience name="{_attr(s.name)}"',
                ]
                if s.directory:
                    parts.append(f' directory="{_attr(s.directory)}"')
                if s.tags:
                    parts.append(f' tags="{_attr(",".join(s.tags))}"')
                if s.scenario:
                    parts.append(f' scenario="{_attr(s.scenario)}"')
                parts.append(">")
                entries.append("".join(parts))
                if s.description:
                    entries.append(f"    <description>{_esc(s.description)}</description>")
                entries.append("  </experience>")
            return "<experiences>\n" + "\n".join(entries) + "\n</experiences>"
        except Exception:
            logger.debug("Failed to build existing experiences XML", exc_info=True)
            return ""

    async def _do_review(
        self, snapshot: str, existing_xml: str, invocation_id: str
    ) -> None:
        before = self._scan_experience_dir()
        try:
            ok = await self._agent.review(
                conversation_snapshot=snapshot,
                experience_dir=self._get_dir(),
                meta_store=self._meta_store,
                existing_experiences=existing_xml,
                invocation_id=invocation_id,
            )
            logger.info(
                "ExperienceReviewHook: review completed invocation=%s ok=%s",
                invocation_id, ok,
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
            after = self._scan_experience_dir()
            await self._cleanup(before, after)

    def _scan_experience_dir(self) -> dict[str, float]:
        """Scan experience directory, return {name: mtime}."""
        result: dict[str, float] = {}
        if not self._get_dir().exists():
            return result
        for entry in self._get_dir().iterdir():
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
                dt = datetime.fromtimestamp(mtime, tz=timezone.utc)
                record.last_used_at = dt.isoformat()
                if record.created_at is None:
                    record.created_at = dt.isoformat()
                self._meta_store.set(name, record)

        # Validate all existing EXPERIENCE.md files
        for name in list(after.keys()):
            md_path = self._get_dir() / name / "EXPERIENCE.md"
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
                dir_path = self._get_dir() / name
                try:
                    if dir_path.exists() and not any(dir_path.iterdir()):
                        dir_path.rmdir()
                except OSError:
                    pass
                self._meta_store.remove(name)
                logger.warning(
                    "Removed invalid experience: %s (%s)", name, result.errors
                )
                continue

            # Auto-correct frontmatter name to match directory name
            auto_correct_frontmatter_name(self._get_dir() / name)

    # -- trigger logic ---------------------------------------------------

    def _should_review(
        self,
        ctx: AgentContext,
        result: AgentResult,
        tool_names: set[str],
    ) -> bool:
        """Gate check before triggering review.

        Gate 1: Turn completed with a plain assistant response
                (stop_reason == "completed").
                Detects "conversation segment completion" — the agent
                finished a conversational exchange without requesting
                tool execution in its final response.
        Gate 2: Detect experience write/edit usage this turn.
                Sets cooldown and returns False so the review doesn't
                run immediately after the agent edits experiences.
        Gate 3: Sufficient conversation history length (>= threshold).
                During cooldown the effective threshold is doubled.
        Gate 4: Non-empty snapshot (checked separately in after_turn).
        """
        # Gate 1: Plain completion
        if result.stop_reason != "completed":
            return False

        # Gate 2: Experience tool usage this turn → cooldown
        if self._detect_exp_edit(tool_names, result):
            logger.debug(
                "ExperienceReviewHook: exp write/edit detected, starting cooldown"
            )
            self._last_exp_tool_turn = self._turn_counter
            return False

        # Gate 3: Sufficient conversation length
        try:
            msg_count = len(ctx.history)
        except Exception:
            return False

        effective_threshold = self._min_messages
        if self._last_exp_tool_turn > 0:
            turns_since = self._turn_counter - self._last_exp_tool_turn
            if turns_since <= self._exp_cooldown_turns:
                effective_threshold = self._min_messages * 2

        if msg_count < effective_threshold:
            return False

        return True

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

    def _capture_snapshot(self, ctx: AgentContext) -> str:
        """Extract recent user/assistant messages as a text snapshot."""
        try:
            messages = list(ctx.history)
        except Exception:
            return ""

        recent = (
            messages[-_SNAPSHOT_MAX_MESSAGES:]
            if len(messages) > _SNAPSHOT_MAX_MESSAGES
            else messages
        )
        lines: list[str] = []
        for m in recent:
            if isinstance(m, dict):
                role = m.get("role", "unknown")
                content = m.get("content", "")
            else:
                role = getattr(m, "role", "unknown")
                content = getattr(m, "content", "")
            if isinstance(content, str) and content.strip():
                if len(content) <= _SNAPSHOT_MAX_CONTENT_LEN:
                    preview = content
                else:
                    preview = content[:_SNAPSHOT_MAX_CONTENT_LEN] + " [truncated]"
                lines.append(f"[{role}]: {preview}")
        return "\n".join(lines)
