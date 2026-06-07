"""Tests for ExperienceReviewHook in pool mode — end-to-end wiring verification.

TDD cycle:
1. RED: test fails because pool_builder.py does not create/register ExperienceReviewHook
2. GREEN: fix pool_builder.py to wire ExperienceReviewHook
3. Verify: reviewer execution produces trace + logs
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from framework.core.provider import LLMProvider
from framework.hook.builtin.experience_review import ExperienceReviewHook


class TestPoolExperienceReviewWiring:
    """Verify ExperienceReviewHook is wired in pool mode."""

    def test_pool_builder_creates_experience_review_hook(self, tmp_path: Path) -> None:
        """When experience.enabled=true, pool_builder must create ExperienceReviewHook."""
        from bot.service.pool_builder import create_pool

        with (
            patch("bot.service.pool_builder.create_llm_provider") as mock_llm,
            patch("bot.service.pool_builder.create_memory") as mock_mem,
            patch("bot.service.pool_builder._build_pool_tool_manager") as mock_tools,
            patch("bot.service.pool_builder._build_pool_skill_manager") as mock_skills,
            patch("bot.service.pool_builder.AgentPool") as mock_pool_cls,
        ):
            mock_llm.return_value = MagicMock(spec=LLMProvider)

            mock_mem_sys = AsyncMock()
            mock_mem_sys.pruned_manager = MagicMock()
            mock_mem.return_value = mock_mem_sys

            mock_tool_mgr = MagicMock()
            mock_tool_mgr.list_tools.return_value = []
            mock_tools.return_value = (mock_tool_mgr, None)

            mock_skills.return_value = None

            mock_pipeline = MagicMock()
            mock_pipeline.hook_runner = MagicMock()
            mock_main_instance = MagicMock()
            mock_main_instance.pipeline = mock_pipeline
            mock_pool = AsyncMock()
            mock_pool._agents = {"testagent": mock_main_instance}
            # AsyncMock attributes are coroutines by default; set to plain list
            mock_pool.list_profiles = MagicMock(return_value=[])
            mock_pool_cls.return_value = mock_pool

            # Build agent config mock with correct .name attribute
            agent_mock = MagicMock()
            agent_mock.name = "testagent"
            agent_mock.role = "main"
            agent_mock.max_steps = 10
            exp_mock = MagicMock()
            exp_mock.enabled = True
            exp_mock.min_messages = 6
            exp_mock.exp_cooldown_turns = 3
            exp_mock.max_iterations = 50
            exp_mock.max_experiences = 20
            exp_mock.curator_interval = 3600
            agent_mock.experience = exp_mock

            asyncio.run(
                create_pool(
                    pool_name="testpool",
                    pool_cfg=MagicMock(
                        llm=MagicMock(model="test", api_key="k", temperature=0.5, max_tokens=1000),
                        memory=MagicMock(),
                        agents=[agent_mock],
                    ),
                    project_dir=tmp_path,
                    data_dir=tmp_path / "data",
                    broker=MagicMock(),
                    inbox_server=MagicMock(),
                    inbox_producer=MagicMock(),
                    inbox_consumer=MagicMock(),
                    agent_bus=MagicMock(),
                    output_adapter=MagicMock(),
                    safety=MagicMock(),
                    retention=MagicMock(),
                    comm_tracker=MagicMock(),
                    approval_workspace=tmp_path / "approval",
                    im_ui=MagicMock(),
                    shared_hooks=[],
                    shared_hook_runner=MagicMock(),
                    shared_interceptor_chain=MagicMock(),
                )
            )

            # Debug: ensure _add_hook was actually invoked
            assert mock_pipeline.hook_runner.add.call_count > 0, (
                "_add_hook was never called — main_instance.pipeline may be None or "
                f"main_instance={mock_pool._agents.get('testagent')}"
            )

            added_hooks = [call.args[0] for call in mock_pipeline.hook_runner.add.call_args_list]
            hook_types = [type(h.hook).__name__ if hasattr(h, "hook") else type(h).__name__ for h in added_hooks]

            assert "ExperienceReviewHook" in hook_types, (
                f"Expected ExperienceReviewHook in pipeline hooks, got: {hook_types}. "
                "Pool mode is missing ExperienceReviewHook wiring!"
            )

    def test_experience_disabled_no_review_hook(self, tmp_path: Path) -> None:
        """When experience.enabled=false, ExperienceReviewHook must NOT be created."""
        from bot.service.pool_builder import create_pool

        with (
            patch("bot.service.pool_builder.create_llm_provider") as mock_llm,
            patch("bot.service.pool_builder.create_memory") as mock_mem,
            patch("bot.service.pool_builder._build_pool_tool_manager") as mock_tools,
            patch("bot.service.pool_builder._build_pool_skill_manager") as mock_skills,
            patch("bot.service.pool_builder.AgentPool") as mock_pool_cls,
        ):
            mock_llm.return_value = MagicMock(spec=LLMProvider)
            mock_mem_sys = AsyncMock()
            mock_mem_sys.pruned_manager = MagicMock()
            mock_mem.return_value = mock_mem_sys

            mock_tool_mgr = MagicMock()
            mock_tool_mgr.list_tools.return_value = []
            mock_tools.return_value = (mock_tool_mgr, None)
            mock_skills.return_value = None

            mock_pipeline = MagicMock()
            mock_pipeline.hook_runner = MagicMock()
            mock_main_instance = MagicMock()
            mock_main_instance.pipeline = mock_pipeline
            mock_pool = AsyncMock()
            mock_pool._agents = {"testagent": mock_main_instance}
            mock_pool.list_profiles = MagicMock(return_value=[])
            mock_pool_cls.return_value = mock_pool

            agent_mock = MagicMock()
            agent_mock.name = "testagent"
            agent_mock.role = "main"
            agent_mock.max_steps = 10
            exp_mock = MagicMock()
            exp_mock.enabled = False
            agent_mock.experience = exp_mock

            asyncio.run(
                create_pool(
                    pool_name="testpool",
                    pool_cfg=MagicMock(
                        llm=MagicMock(model="test", api_key="k", temperature=0.5, max_tokens=1000),
                        memory=MagicMock(),
                        agents=[agent_mock],
                    ),
                    project_dir=tmp_path,
                    data_dir=tmp_path / "data",
                    broker=MagicMock(),
                    inbox_server=MagicMock(),
                    inbox_producer=MagicMock(),
                    inbox_consumer=MagicMock(),
                    agent_bus=MagicMock(),
                    output_adapter=MagicMock(),
                    safety=MagicMock(),
                    retention=MagicMock(),
                    comm_tracker=MagicMock(),
                    approval_workspace=tmp_path / "approval",
                    im_ui=MagicMock(),
                    shared_hooks=[],
                    shared_hook_runner=MagicMock(),
                    shared_interceptor_chain=MagicMock(),
                )
            )

            added_hooks = [call.args[0] for call in mock_pipeline.hook_runner.add.call_args_list]
            hook_types = [type(h).__name__ for h in added_hooks]
            assert "ExperienceReviewHook" not in hook_types


class TestExperienceReviewHookExecution:
    """Verify ExperienceReviewHook triggers review with trace + logging."""

    @pytest.mark.asyncio
    async def test_review_agent_produces_jsonl_trace(self, tmp_path: Path) -> None:
        """When review runs, it must write a JSONL trace file."""
        from framework.agents.experience.review_agent import ExperienceReviewAgent
        from framework.core.experience.meta import PerFileExperienceMetaStore
        from framework.core.provider import LLMProvider

        provider = MagicMock(spec=LLMProvider)
        agent = ExperienceReviewAgent(provider=provider, max_iterations=2)

        exp_dir = tmp_path / "experiences"
        exp_dir.mkdir(parents=True, exist_ok=True)
        meta = PerFileExperienceMetaStore(exp_dir)

        # Mock the ReActAgent.run so we don't need a real LLM,
        # but make it emit a complete event so trace is written.
        async def _mock_run(context, emitter):
            from framework.core.emitter import AgentResult
            from framework.core.constants import StopReason
            await emitter.emit_complete(AgentResult(content="done", stop_reason=StopReason.COMPLETED))
            return AgentResult(content="done", stop_reason=StopReason.COMPLETED)

        agent._react_agent = MagicMock()
        agent._react_agent.run = _mock_run

        await agent.review(
            conversation_snapshot="[user]: hello\n[assistant]: hi",
            experience_dir=exp_dir,
            meta_store=meta,
            existing_experiences="",
            invocation_id="test-inv",
        )

        # trace now goes to experience_dir.parent / "review_traces" by default
        trace_dir = exp_dir.parent / "review_traces"
        trace_files = list(trace_dir.glob("review-*.jsonl"))
        assert len(trace_files) == 1, (
            f"Expected 1 trace file, got {len(trace_files)}. "
            "SummarizerTrajectoryEmitter should produce JSONL trace."
        )

    @pytest.mark.asyncio
    async def test_review_agent_has_run_logging_hook(self, tmp_path: Path) -> None:
        """Review agent's internal ReAct loop must have RunLoggingHook for observability."""
        from framework.agents.experience.review_agent import ExperienceReviewAgent
        from framework.core.experience.meta import PerFileExperienceMetaStore
        from framework.core.provider import LLMProvider

        provider = MagicMock(spec=LLMProvider)
        agent = ExperienceReviewAgent(provider=provider, max_iterations=2)

        exp_dir = tmp_path / "experiences"
        exp_dir.mkdir(parents=True, exist_ok=True)
        meta = PerFileExperienceMetaStore(exp_dir)

        async def _mock_run(context, emitter):
            from framework.core.emitter import AgentResult
            from framework.core.constants import StopReason
            await emitter.emit_complete(AgentResult(content="done", stop_reason=StopReason.COMPLETED))
            return AgentResult(content="done", stop_reason=StopReason.COMPLETED)

        agent._react_agent = MagicMock()
        agent._react_agent.run = _mock_run

        with patch("framework.hook.runner.HookRunner") as MockRunner:
            mock_runner = MagicMock()
            MockRunner.return_value = mock_runner

            await agent.review(
                conversation_snapshot="[user]: hello\n[assistant]: hi",
                experience_dir=exp_dir,
                meta_store=meta,
            )

            added_specs = [call.args[0] for call in mock_runner.add.call_args_list]
            hook_classes = [type(spec.hook).__name__ for spec in added_specs]
            assert "RunLoggingHook" in hook_classes, (
                f"Expected RunLoggingHook for observability, got: {hook_classes}"
            )

    @pytest.mark.asyncio
    async def test_experience_review_hook_after_turn_logs_info(self, tmp_path: Path) -> None:
        """ExperienceReviewHook.after_turn must log INFO when triggering review."""
        from framework.agents.experience.review_agent import ExperienceReviewAgent
        from framework.core.experience.meta import PerFileExperienceMetaStore
        from framework.core.emitter import AgentResult
        from framework.core.constants import StopReason
        from framework.memory.history import ListMessageHistory

        exp_dir = tmp_path / "experiences"
        exp_dir.mkdir(parents=True, exist_ok=True)
        meta = PerFileExperienceMetaStore(exp_dir)

        review_agent = MagicMock(spec=ExperienceReviewAgent)
        review_agent.review = AsyncMock(return_value=True)

        hook = ExperienceReviewHook(
            review_agent=review_agent,
            experience_dir=exp_dir,
            meta_store=meta,
            min_messages=2,
            exp_cooldown_turns=3,
        )
        hook._turn_counter = 10

        ctx = MagicMock()
        ctx.history = ListMessageHistory([
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
            {"role": "user", "content": "world"},
            {"role": "assistant", "content": "earth"},
        ])

        result = AgentResult(content="earth", stop_reason=StopReason.COMPLETED, messages=[])

        records: list[logging.LogRecord] = []
        logger = logging.getLogger("framework.hook.builtin.experience_review")
        old_level = logger.level
        logger.setLevel(logging.INFO)

        class _ListHandler(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                records.append(record)

        handler = _ListHandler()
        logger.addHandler(handler)

        try:
            await hook.after_turn(ctx, result)
            await asyncio.sleep(0.15)
        finally:
            logger.removeHandler(handler)
            logger.setLevel(old_level)

        info_messages = [r.getMessage() for r in records if r.levelno == logging.INFO]
        assert any("ExperienceReviewHook: triggering review" in m for m in info_messages), (
            f"Expected INFO log about triggering review, got: {info_messages}"
        )
