from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from modex_agent.agents.summarizer.abc import _get_registry
from modex_agent.agents.summarizer.scoped_file_agent import ScopedFileAgent
from modex_agent.core.experience import ExperienceMetaStore
from modex_agent.core.message import ChatMessage
from modex_agent.core.provider import LLMProvider
from modex_agent.core.session_id import SessionInfo
from modex_agent.memory.tools.experience import (
    ExperienceDeleteTool,
    ExperienceEditTool,
    ExperienceListTool,
    ExperienceReadTool,
    ExperienceRenameDirTool,
    ExperienceWriteTool,
)

logger = logging.getLogger(__name__)


class ExperienceReviewAgent(ScopedFileAgent):
    """ReAct agent for reviewing conversations and managing experiences.

    Uses the unified experience tools (same as main agent).
    Follows ScopedFileAgent pattern with custom tool assembly.
    Observability via SummarizerTrajectoryEmitter (JSONL trace + logging).
    """

    DEFAULT_MAX_ITERATIONS = 100

    def __init__(
        self,
        provider: LLMProvider,
        max_iterations: int = DEFAULT_MAX_ITERATIONS,
    ) -> None:
        super().__init__(provider=provider, max_iterations=max_iterations)

    @staticmethod
    def build_system_prompt(experience_dir: Path) -> str:
        return _get_registry().get_system(
            "experience/review",
            allowed_dir=str(experience_dir.resolve()),
        )

    @staticmethod
    def build_user_message(
        conversation_snapshot: str,
        existing_experiences: str = "",
    ) -> str:
        template = _get_registry().get_user(
            "experience/review",
            conversation_snapshot="__CONVERSATION_SNAPSHOT__",
            existing_experiences="__EXISTING_EXPERIENCES__",
        )
        template = template.replace("__CONVERSATION_SNAPSHOT__", conversation_snapshot)
        template = template.replace("__EXISTING_EXPERIENCES__", existing_experiences)
        return template

    async def review(
        self,
        conversation_snapshot: str,
        experience_dir: Path,
        meta_store: ExperienceMetaStore,
        *,
        existing_experiences: str = "",
        invocation_id: str = "",
        traces_dir: Path | None = None,
        conversation_messages: Sequence[ChatMessage] | None = None,
    ) -> bool:
        """Main entry: review conversation and create/update experiences.

        Args:
            conversation_snapshot: Formatted conversation history (text). Used
                as the review instruction body when ``conversation_messages``
                is absent; ignored (may be empty) when fork mode is active.
            experience_dir: Target directory for EXPERIENCE.md files.
            meta_store: Metadata store for lifecycle management.
            existing_experiences: Pre-formatted XML of existing experiences.
            invocation_id: Caller-supplied UUID for trace correlation.
            traces_dir: Directory for JSONL trace files.  If None, defaults to
                ``experience_dir.parent / "review_traces"`` so traces never
                pollute the experience data directory.
            conversation_messages: Forked parent-session messages (fork mode).
                When provided, the reviewer's ReAct history is seeded with
                these messages (preserving tool_calls / tool_results /
                structured content) followed by the review instruction user
                message. When ``None`` or empty, falls back to embedding
                ``conversation_snapshot`` in a single user message (legacy).

        Returns:
            True if agent ran successfully, False otherwise.
        """
        fork_active = bool(conversation_messages)
        if not fork_active and not conversation_snapshot.strip():
            return True

        system_prompt = self.build_system_prompt(experience_dir)
        user_msg = self.build_user_message(conversation_snapshot, existing_experiences)

        trace_key = invocation_id or uuid.uuid4().hex[:8]
        session_id = f"{trace_key}.experience-review"
        _td = traces_dir or (experience_dir.parent / "review_traces")
        trace_path = _td / f"{trace_key}" / "trajectory.jsonl"

        logger.info(
            "ExperienceReviewAgent starting: invocation=%s session=%s fork=%s",
            invocation_id or trace_key,
            session_id,
            fork_active,
        )

        for attempt in range(2):
            ok = await self._run_agent_with_experience_tools(
                system_prompt=system_prompt,
                user_msg=user_msg,
                experience_dir=experience_dir,
                meta_store=meta_store,
                session_id=session_id,
                agent_name="ExperienceReviewAgent",
                trace_path=trace_path,
                max_iterations=self.max_iterations,
                conversation_messages=conversation_messages if fork_active else None,
            )
            if ok:
                logger.info(
                    "ExperienceReviewAgent succeeded: invocation=%s attempt=%d",
                    invocation_id or trace_key,
                    attempt + 1,
                )
                return True
            logger.warning(
                "ExperienceReviewAgent attempt %d failed invocation=%s",
                attempt + 1,
                invocation_id or trace_key,
            )

        logger.error(
            "ExperienceReviewAgent all attempts failed: invocation=%s",
            invocation_id or trace_key,
        )
        return False

    async def _run_agent_with_experience_tools(
        self,
        *,
        system_prompt: str,
        user_msg: str,
        experience_dir: Path,
        meta_store: ExperienceMetaStore,
        session_id: str,
        agent_name: str,
        trace_path: Path,
        max_iterations: int,
        temperature: float = 0.2,
        conversation_messages: Sequence[ChatMessage] | None = None,
    ) -> bool:
        """Run ReAct agent with experience-specific tools.

        When ``conversation_messages`` is provided (fork mode), the reviewer's
        history is seeded with the parent session's messages — preserving
        tool_calls, tool_results, and structured content — followed by the
        review instruction user message. Otherwise a single-user-message
        history is used (legacy text-snapshot mode).
        """
        from modex_agent.agents.react.state import ReActTurnState
        from modex_agent.agents.summarizer.emitter import SummarizerTrajectoryEmitter
        from modex_agent.core.agent import AgentContext
        from modex_agent.core.tool_manager import InMemoryToolManager
        from modex_agent.core.types import MessageRole
        from modex_agent.hook.abc import HookErrorPolicy, HookSpec
        from modex_agent.hook.builtin import RunLoggingHook
        from modex_agent.hook.runner import HookRunner
        from modex_agent.memory.history import ListMessageHistory
        from modex_agent.runtime.enums import AgentKind, TurnPhase
        from modex_agent.runtime.models import TurnIdentity
        from modex_agent.runtime.services import AgentRuntime, AgentRuntimeServices

        tools = [
            ExperienceReadTool(experience_dir, meta_store),
            ExperienceWriteTool(experience_dir, meta_store),
            ExperienceEditTool(experience_dir, meta_store),
            ExperienceListTool(experience_dir, meta_store),
            ExperienceRenameDirTool(experience_dir, meta_store),
            ExperienceDeleteTool(experience_dir, meta_store),
        ]

        tool_manager = InMemoryToolManager()
        for tool in tools:
            tool_manager.register(tool)

        # Build the seed history. In fork mode, prepend the parent session's
        # messages (preserving tool_calls / tool_results / structured content)
        # so the reviewer can inspect the real conversation via ReAct. The
        # review instruction is appended as the final user message.
        seed: list[dict[str, Any]] = []
        if conversation_messages:
            for msg in conversation_messages:
                if isinstance(msg, ChatMessage):
                    seed.append(msg.model_dump())
                elif isinstance(msg, dict):
                    seed.append(dict(msg))
        seed.append({"role": MessageRole.USER, "content": user_msg})
        history = ListMessageHistory(seed)

        # HookRunner with RunLoggingHook so ReActAgent dispatches
        # BEFORE_TURN / AFTER_TURN / tool hooks for observability.
        hook_runner = HookRunner()
        hook_runner.add(
            HookSpec(
                hook=RunLoggingHook(
                    logger_name="experience.review.agent",
                    level=logging.DEBUG,
                    max_content_chars=2000,
                    max_result_chars=2000,
                ),
                on_error=HookErrorPolicy.LOG,
            )
        )

        # Pre-built runtime — ReActAgent.run() will use it directly
        # instead of creating a default empty one.
        state = ReActTurnState(
            identity=TurnIdentity(
                agent_id="experience-review",
                session=SessionInfo(session_id=session_id, agent_name=agent_name),
                turn_id="default",
            ),
            agent_kind=AgentKind.REACT,
            phase=TurnPhase.CREATED,
        )
        runtime = AgentRuntime(
            services=AgentRuntimeServices(hooks=hook_runner),
            state=state,
        )

        context = AgentContext(
            system_prompt=system_prompt,
            history=history,
            tool_manager=tool_manager,
            session=SessionInfo(session_id=session_id, agent_name=agent_name),
            max_iterations=max_iterations,
            temperature=temperature,
            runtime=runtime,
            identity=state.identity,
        )

        trace_path.parent.mkdir(parents=True, exist_ok=True)
        emitter = SummarizerTrajectoryEmitter(
            session_id=session_id,
            agent_name=agent_name,
            trace_path=trace_path,
        )

        try:
            await self._react_agent.run(context, emitter)
            return True
        except Exception:
            logger.exception("%s execution error", agent_name)
            return False
