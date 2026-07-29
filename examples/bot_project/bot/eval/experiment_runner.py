"""Experiment runner — run ReActAgent against Langfuse datasets.

Wraps ``ReActAgent(provider, mode='clean')`` as a Langfuse experiment task
function. Each dataset item gets a fresh ``AgentContext`` (zero state leakage
between items); the agent instance is reused (stateless in clean mode).

Layer 2 of the eval architecture (ADR-0024, IN15 step 6): dataset -> runner ->
traces + scores. Runs as a separate process (opt-in via the ``[eval]`` extra)
to avoid OTel tracer-provider conflicts with the bot's JSON-OTLP trace path.

Usage::

    runner = EvalRunner(
        provider=my_llm_provider,
        system_prompt="You are a helpful assistant.",
        langfuse_client=Langfuse(host=..., public_key=..., secret_key=...),
    )
    result = runner.run(
        dataset_name="react-baseline",
        experiment_name="v1",
        evaluators=[exact_match_evaluator],
    )
    print(result.format())
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from langfuse import Langfuse

if TYPE_CHECKING:
    from langfuse.experiment import ExperimentResult

from modex_agent.agents.react.agent import ReActAgent, ReActEvent
from modex_agent.core.agent import AgentContext
from modex_agent.core.constants import StopReason
from modex_agent.core.emitter import AgentResult, ContentEmitter
from modex_agent.core.message import ChatMessage
from modex_agent.core.provider import LLMProvider
from modex_agent.core.session_id import SessionInfo
from modex_agent.core.tool_manager import InMemoryToolManager
from modex_agent.core.types import MessageRole
from modex_agent.memory.history import ListMessageHistory

logger = logging.getLogger(__name__)


class _NoopEmitter(ContentEmitter[ReActEvent]):
    """Minimal emitter that discards all events.

    Eval runs consume only the final ``AgentResult``; streaming events are
    not needed. All abstract methods are no-ops.
    """

    async def emit_delta(self, delta: str) -> None:
        pass

    async def emit_complete(self, result: AgentResult) -> None:
        pass

    async def emit_error(self, error: str) -> None:
        pass


class EvalRunner:
    """Run ReActAgent experiments against Langfuse datasets.

    Wraps ``ReActAgent(provider, mode='clean')`` as a Langfuse experiment
    task. Constructs a fresh ``AgentContext`` per item (zero state leakage
    between items). The agent instance is reused -- in clean mode it is
    stateless across turns.
    """

    def __init__(
        self,
        *,
        provider: LLMProvider,
        system_prompt: str,
        max_iterations: int = 10,
        langfuse_client: Langfuse | None = None,
    ) -> None:
        """Store config and build the clean-mode ReActAgent.

        Args:
            provider: LLM provider for the agent.
            system_prompt: System prompt applied to every eval item.
            max_iterations: ReAct loop cap per item.
            langfuse_client: Optional pre-built Langfuse client. When
                ``None``, ``Langfuse()`` is constructed from env vars
                (``LANGFUSE_HOST`` etc.) at ``run`` time.
        """
        self._agent = ReActAgent(provider, mode="clean")
        self._system_prompt = system_prompt
        self._max_iterations = max_iterations
        self._lf = langfuse_client

    async def task(self, *, item: object, **kwargs: object) -> dict[str, Any]:
        """Langfuse experiment task function (async).

        Builds a fresh ``AgentContext`` per item, runs the agent, and returns
        structured output for Langfuse scoring. Failures are caught and
        returned as structured errors rather than raised -- this keeps the
        Langfuse trace intact for inspection.

        Args:
            item: A Langfuse ``DatasetItem`` with ``.id`` and ``.input``.
            **kwargs: Additional keyword args passed through by Langfuse.

        Returns:
            Dict with ``output``, ``stop_reason``, and ``error`` keys.
        """
        raw_input = getattr(item, "input", None)
        if isinstance(raw_input, dict):
            query = str(raw_input.get("query") or "")
        elif raw_input is not None:
            query = str(raw_input)
        else:
            query = ""

        item_id = getattr(item, "id", "unknown")
        ctx = AgentContext(
            system_prompt=self._system_prompt,
            history=ListMessageHistory(),
            tool_manager=InMemoryToolManager(),
            session=SessionInfo.from_str(f"eval.{item_id}.react"),
            max_iterations=self._max_iterations,
        )
        await ctx.history.append(ChatMessage(role=MessageRole.USER, content=query))

        emitter = _NoopEmitter()
        try:
            result = await self._agent.run(ctx, emitter)
        except Exception as exc:
            logger.warning(
                "EvalRunner: agent run failed for item %s",
                item_id,
                exc_info=True,
            )
            return {
                "output": "",
                "stop_reason": str(StopReason.ERROR),
                "error": str(exc),
            }
        return {
            "output": result.content or "",
            "stop_reason": str(result.stop_reason),
            "error": result.error,
        }

    def run(
        self,
        *,
        dataset_name: str,
        experiment_name: str,
        description: str = "",
        evaluators: list[Any] | None = None,
        max_concurrency: int = 5,
    ) -> ExperimentResult:
        """Run an experiment against a Langfuse dataset.

        The async ``task`` method is invoked by the Langfuse SDK per dataset
        item (the SDK manages the event loop internally). Each item produces
        a trace plus scores from the supplied evaluators.

        Args:
            dataset_name: Name of the Langfuse dataset to run against.
            experiment_name: Human-readable name for this experiment run.
            description: Optional experiment description.
            evaluators: Langfuse evaluator callables. Each is called with the
                task output (and expected output when present) and returns an
                ``Evaluation``.
            max_concurrency: Maximum concurrent item executions.

        Returns:
            The Langfuse experiment result object -- call ``.format()`` for
            a human-readable summary.
        """
        lf = self._lf or Langfuse()
        dataset = lf.get_dataset(dataset_name)
        result = dataset.run_experiment(
            name=experiment_name,
            description=description,
            task=self.task,
            evaluators=evaluators or [],
            max_concurrency=max_concurrency,
        )
        return result


__all__ = ["EvalRunner"]
