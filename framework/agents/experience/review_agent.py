from __future__ import annotations

import logging
import uuid
from pathlib import Path

from framework.agents.summarizer.abc import _get_registry
from framework.agents.summarizer.scoped_file_agent import ScopedFileAgent
from framework.core.experience.meta import ExperienceMetaStore
from framework.core.provider import LLMProvider
from framework.core.session_id import SessionId
from framework.memory.tools.experience import (
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
    ) -> bool:
        """Main entry: review conversation and create/update experiences.

        Args:
            conversation_snapshot: Formatted conversation history.
            experience_dir: Target directory for EXPERIENCE.md files.
            meta_store: Metadata store for lifecycle management.
            existing_experiences: Pre-formatted XML of existing experiences.
            invocation_id: Caller-supplied UUID for trace correlation.
            traces_dir: Directory for JSONL trace files.  If None, defaults to
                ``experience_dir.parent / "review_traces"`` so traces never
                pollute the experience data directory.

        Returns:
            True if agent ran successfully, False otherwise.
        """
        if not conversation_snapshot.strip():
            return True

        system_prompt = self.build_system_prompt(experience_dir)
        user_msg = self.build_user_message(conversation_snapshot, existing_experiences)

        trace_key = invocation_id or uuid.uuid4().hex[:8]
        session_id = f"experience-review-{trace_key}"
        _td = traces_dir or (experience_dir.parent / "review_traces")
        trace_path = _td / f"review-{trace_key}.jsonl"

        logger.info(
            "ExperienceReviewAgent starting: invocation=%s session=%s",
            invocation_id or trace_key,
            session_id,
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
    ) -> bool:
        """Run ReAct agent with experience-specific tools."""
        from framework.agents.react.state import ReActTurnState
        from framework.agents.summarizer.emitter import SummarizerTrajectoryEmitter
        from framework.core.agent import AgentContext
        from framework.core.tool_manager import InMemoryToolManager
        from framework.core.types import MessageRole
        from framework.hook.abc import HookErrorPolicy, HookSpec
        from framework.hook.builtin import RunLoggingHook
        from framework.hook.runner import HookRunner
        from framework.memory.history import ListMessageHistory
        from framework.runtime.enums import AgentKind, TurnPhase
        from framework.runtime.models import TurnIdentity
        from framework.runtime.services import AgentRuntime, AgentRuntimeServices

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

        history = ListMessageHistory(
            [
                {"role": MessageRole.USER, "content": user_msg},
            ]
        )

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
                session=SessionId(session_id=session_id, agent_name=agent_name),
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
            session=SessionId.from_str(session_id),
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
