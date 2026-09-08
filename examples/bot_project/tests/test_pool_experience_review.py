"""Tests for ExperienceReviewHook in pool mode — end-to-end wiring verification.

TDD cycle:
1. RED: test fails because pool_builder.py does not create/register ExperienceReviewHook
2. GREEN: fix pool_builder.py to wire ExperienceReviewHook
3. Verify: reviewer execution produces trace + logs
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from modex_agent.core.message import ChatMessage, MessageRole
from modex_agent.core.session_id import SessionInfo
from modex_agent.memory.core.system import MemorySystem
from modex_agent.plugins.defaults.capabilities.experience.review_hook import ExperienceReviewHook


class TestExperienceReviewHookExecution:
    """Verify ExperienceReviewHook triggers review with trace + logging."""

    @pytest.mark.asyncio
    async def test_review_agent_produces_jsonl_trace(self, tmp_path: Path) -> None:
        """When review runs, it must write a JSONL trace file."""
        from modex_agent.core.provider import LLMProvider
        from modex_agent.plugins.defaults.capabilities.experience.metadata import (
            PerFileExperienceMetaStore,
        )
        from modex_agent.plugins.defaults.capabilities.experience.reviewer import (
            ExperienceReviewAgent,
        )

        provider = MagicMock(spec=LLMProvider)
        agent = ExperienceReviewAgent(provider=provider, max_iterations=2)

        exp_dir = tmp_path / "experiences"
        exp_dir.mkdir(parents=True, exist_ok=True)
        meta = PerFileExperienceMetaStore(exp_dir)

        # Mock the ReActAgent.run so we don't need a real LLM,
        # but make it emit a complete event so trace is written.
        async def _mock_run(context, emitter):
            from modex_agent.core.emitter import AgentResult, StopReason

            await emitter.emit_complete(
                AgentResult(content="done", stop_reason=StopReason.COMPLETED)
            )
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

        # trace layout: experience_dir.parent / "review_traces" / {trace_key} / trajectory.jsonl
        # (trace_key = invocation_id when provided; traces never pollute the experience dir)
        trace_key = "test-inv"
        trace_file = exp_dir.parent / "review_traces" / trace_key / "trajectory.jsonl"
        review_traces = exp_dir.parent / "review_traces"
        assert trace_file.exists(), (
            f"Expected trace file at {trace_file}, not found. "
            "SummarizerTrajectoryEmitter should produce JSONL trace. "
            f"review_traces tree: {list(review_traces.rglob('*')) if review_traces.exists() else '<missing>'}"
        )

    @pytest.mark.asyncio
    async def test_review_agent_has_run_logging_hook(self, tmp_path: Path) -> None:
        """Review agent's internal ReAct loop must have RunLoggingHook for observability."""
        from modex_agent.core.provider import LLMProvider
        from modex_agent.plugins.defaults.capabilities.experience.metadata import (
            PerFileExperienceMetaStore,
        )
        from modex_agent.plugins.defaults.capabilities.experience.reviewer import (
            ExperienceReviewAgent,
        )

        provider = MagicMock(spec=LLMProvider)
        agent = ExperienceReviewAgent(provider=provider, max_iterations=2)

        exp_dir = tmp_path / "experiences"
        exp_dir.mkdir(parents=True, exist_ok=True)
        meta = PerFileExperienceMetaStore(exp_dir)

        async def _mock_run(context, emitter):
            from modex_agent.core.emitter import AgentResult, StopReason

            await emitter.emit_complete(
                AgentResult(content="done", stop_reason=StopReason.COMPLETED)
            )
            return AgentResult(content="done", stop_reason=StopReason.COMPLETED)

        agent._react_agent = MagicMock()
        agent._react_agent.run = _mock_run

        with patch("modex_agent.hook.runner.HookRunner") as mock_runner_cls:
            mock_runner = MagicMock()
            mock_runner_cls.return_value = mock_runner

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
    async def test_experience_review_hook_after_graph_logs_info(self, tmp_path: Path) -> None:
        """ExperienceReviewHook.after_graph must log INFO when triggering review."""
        from modex_agent.core.emitter import AgentResult, StopReason
        from modex_agent.memory.history import ListMessageHistory
        from modex_agent.plugins.defaults.capabilities.experience.metadata import (
            PerFileExperienceMetaStore,
        )
        from modex_agent.plugins.defaults.capabilities.experience.reviewer import (
            ExperienceReviewAgent,
        )

        exp_dir = tmp_path / "experiences"
        exp_dir.mkdir(parents=True, exist_ok=True)
        meta = PerFileExperienceMetaStore(exp_dir)

        review_agent = MagicMock(spec=ExperienceReviewAgent)
        review_agent.review = AsyncMock(return_value=True)
        memory_system = MagicMock(spec=MemorySystem)
        memory_system.get_full_history = AsyncMock(
            return_value=[ChatMessage(role=MessageRole.USER, content="full history")]
        )

        from modex_agent.plugins.defaults.capabilities.experience.catalog import (
            ExperienceCatalog,
        )
        from modex_agent.plugins.defaults.capabilities.experience.config import (
            ExperiencePoolConfig,
            ExperienceReviewConfig,
        )
        from modex_agent.plugins.defaults.capabilities.experience.supply import (
            ExperienceSupply,
        )

        supply = ExperienceSupply(
            pool_name="p",
            catalog=ExperienceCatalog(experience_dir=exp_dir, meta_store=meta),
            experience_dir=exp_dir,
            meta_store=meta,
            pool_config=ExperiencePoolConfig(),
            review_config_by_agent={"main": ExperienceReviewConfig()},
            review_provider=MagicMock(),
        )
        supply.register_review_agent("main", review_agent)
        hook = ExperienceReviewHook(
            agent_name="main",
            supply=supply,
            memory_system=memory_system,
            catalog=supply.catalog,
            min_messages=2,
            exp_cooldown_turns=3,
        )

        ctx = MagicMock()
        ctx.session = SessionInfo.from_str("pool-review.main")
        ctx.history = ListMessageHistory(
            [
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "hi"},
                {"role": "user", "content": "world"},
                {"role": "assistant", "content": "earth"},
            ]
        )

        result = AgentResult(content="earth", stop_reason=StopReason.COMPLETED, messages=[])

        records: list[logging.LogRecord] = []
        logger = logging.getLogger("modex_agent.plugins.defaults.capabilities.experience.review_hook")
        old_level = logger.level
        logger.setLevel(logging.INFO)

        class _ListHandler(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                records.append(record)

        handler = _ListHandler()
        logger.addHandler(handler)

        try:
            await supply.start()
            await hook.after_graph(ctx, result)
            await asyncio.sleep(0.15)
        finally:
            await supply.stop()
            logger.removeHandler(handler)
            logger.setLevel(old_level)

        info_messages = [r.getMessage() for r in records if r.levelno == logging.INFO]
        assert any("ExperienceReviewHook: triggering review" in m for m in info_messages), (
            f"Expected INFO log about triggering review, got: {info_messages}"
        )
