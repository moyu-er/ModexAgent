from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from framework.agents.experience.review_agent import ExperienceReviewAgent
from framework.core.experience.meta import ExperienceMetaStore
from framework.core.experience.name_sync import auto_correct_frontmatter_name
from framework.core.experience.source import FileExperienceSource
from framework.core.experience.validation import validate_experience_md

logger = logging.getLogger(__name__)

_SNAPSHOT_MAX_MESSAGES = 20
_SNAPSHOT_MAX_CONTENT_LEN = 1200


class ExperienceReviewHook:
    """AfterTurn hook that spawns an ExperienceReviewAgent.

    Features:
    - 5-gate check (interval, tool_calls, msg_count>=6, non-empty snapshot,
      no recent experience_write/edit)
    - Async mutex: only one review at a time
    - Post-review cleanup: validate, remove invalid, fix dir-name mismatches
    - Pending task tracking with cancel_pending() for workspace switch
    """

    # Tool names that indicate the agent is already managing experiences
    _EXP_EDIT_TOOL_NAMES = frozenset({
        "experience_write",
        "experience_edit",
        "experience",
    })

    def __init__(
        self,
        review_agent: ExperienceReviewAgent,
        experience_dir: Path | Callable[[], Path],
        meta_store: ExperienceMetaStore,
        review_interval: int = 5,
    ) -> None:
        self._agent = review_agent
        self._get_dir = experience_dir if callable(experience_dir) else lambda: experience_dir
        self._meta_store = meta_store
        self._interval = review_interval
        self._pending: set[asyncio.Task] = set()
        self._recent_exp_edit = False

    @property
    def name(self) -> str:
        return "experience_review_hook"

    # -- hook lifecycle --------------------------------------------------

    async def after_turn(self, ctx: Any, result: Any = None) -> None:
        # Gate 0: async mutex
        if self._pending:
            logger.debug(
                "ExperienceReviewHook: skipped — review already in progress"
            )
            return

        turn_count = getattr(ctx, "turn_count", 0)
        if not self._should_review(ctx):
            logger.debug(
                "ExperienceReviewHook: skipped turn=%s interval=%s",
                turn_count, self._interval,
            )
            return

        snapshot = self._capture_snapshot(ctx)
        if not snapshot:
            logger.debug(
                "ExperienceReviewHook: skipped (empty snapshot) turn=%s",
                turn_count,
            )
            return

        # Gather existing experiences for user message
        existing_xml = await self._build_existing_experiences_xml()

        invocation_id = uuid.uuid4().hex
        logger.info(
            "ExperienceReviewHook: triggering review invocation=%s turn=%s",
            invocation_id, turn_count,
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

    def _should_review(self, ctx: Any) -> bool:
        """5-gate check before triggering review.

        Gate 1: turn_count is a multiple of the interval.
        Gate 2: the turn produced at least one tool call (not pure chat).
        Gate 3: the conversation is non-trivial in length (>= 6 messages).
        Gate 4: non-empty snapshot is checked separately.
        Gate 5: the agent has NOT used experience_write or experience_edit
                in this session.  If it just did, set the flag now and skip.
        """
        turn_count = getattr(ctx, "turn_count", 0)
        if turn_count <= 0 or turn_count % self._interval != 0:
            return False

        # Gate 2: has tool calls
        tool_calls = getattr(ctx, "tool_calls_this_turn", None)
        if tool_calls is None:
            tool_calls = getattr(ctx, "_tool_calls", None)
        has_tool_calls = bool(tool_calls)
        if not has_tool_calls:
            rt = getattr(ctx, "runtime", None)
            if rt is not None and hasattr(rt, "has_called"):
                has_tool_calls = True

        # Gate 3: sufficient conversation length
        history = getattr(ctx, "history", None)
        if history is None:
            return False
        try:
            messages = history.messages if hasattr(history, "messages") else []
            msg_count = len(messages)
        except Exception:
            return False
        if msg_count < 6:
            return False

        # Gate 5: detect experience write/edit usage this turn
        if self._detect_exp_edit_usage(tool_calls):
            logger.debug("ExperienceReviewHook: skipped — exp write/edit detected this turn")
            return False
        if self._recent_exp_edit:
            logger.debug("ExperienceReviewHook: skipped — recent exp edit in session")
            return False

        return True

    def _detect_exp_edit_usage(self, tool_calls: Any) -> bool:
        """Check tool calls for experience write/edit usage.

        Sets ``_recent_exp_edit`` and returns True if this turn used
        experience_write, experience_edit, or experience with write/edit action.
        """
        if not tool_calls:
            return False
        for tc in tool_calls:
            tool_name = getattr(tc, "tool_name", None)
            if tool_name is None:
                continue
            if tool_name in ("experience_write", "experience_edit"):
                self._recent_exp_edit = True
                return True
            if tool_name == "experience":
                args = getattr(tc, "arguments", None)
                if isinstance(args, dict) and args.get("action") in ("write", "edit"):
                    self._recent_exp_edit = True
                    return True
        return False

    def _capture_snapshot(self, ctx: Any) -> str:
        """Extract recent user/assistant messages as a text snapshot."""
        history = getattr(ctx, "history", None)
        if history is None:
            return ""

        try:
            messages = history.messages if hasattr(history, "messages") else []
        except Exception:
            return ""

        recent = (
            list(messages)[-_SNAPSHOT_MAX_MESSAGES:]
            if len(messages) > _SNAPSHOT_MAX_MESSAGES
            else list(messages)
        )
        lines: list[str] = []
        for m in recent:
            role = getattr(m, "role", "unknown")
            content = getattr(m, "content", "")
            if isinstance(content, str) and content.strip():
                if len(content) <= _SNAPSHOT_MAX_CONTENT_LEN:
                    preview = content
                else:
                    preview = content[:_SNAPSHOT_MAX_CONTENT_LEN] + " [truncated]"
                lines.append(f"[{role}]: {preview}")
        return "\n".join(lines)
