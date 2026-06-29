"""Regression: post-construction reassignment of ``pipeline.emitter_factory``
must propagate to ``TurnContextBuilder``.

``pool_builder._create_with_emitter`` (examples/bot_project/bot/service/
pool_builder.py:711-719) reassigns ``pipeline.emitter_factory`` /
``workspace_manager`` / ``pool_name`` AFTER ``AgentPipeline.__init__`` runs.
The TurnContextBuilder captures ``emitter_factory`` eagerly at construction,
so without a mirroring setter the builder's copy goes stale and the pool-
specific emitter is lost. This test pins the mirror.
"""
from __future__ import annotations

from unittest.mock import MagicMock

from modex_agent.pipeline.pipeline import AgentPipeline


def _make_pipeline(emitter_factory=None) -> AgentPipeline:
    return AgentPipeline(
        agent=MagicMock(),
        context_manager=MagicMock(),
        tool_manager=MagicMock(),
        input_adapter=MagicMock(),
        output_adapter=MagicMock(),
        emitter_factory=emitter_factory,
    )


def test_emitter_factory_initial_capture():
    factory_a = MagicMock(name="emitter_a")
    pipeline = _make_pipeline(emitter_factory=factory_a)
    assert pipeline.emitter_factory is factory_a
    assert pipeline._turn_context_builder._emitter_factory is factory_a


def test_emitter_factory_post_construction_mutation_propagates_to_builder():
    """Mirrors pool_builder.py:715 — pipeline.emitter_factory = <new> after init."""
    pipeline = _make_pipeline(emitter_factory=MagicMock(name="emitter_a"))

    factory_b = MagicMock(name="emitter_b")
    pipeline.emitter_factory = factory_b

    assert pipeline.emitter_factory is factory_b
    # The builder must see the updated value, not the stale construction-time capture.
    assert pipeline._turn_context_builder._emitter_factory is factory_b
