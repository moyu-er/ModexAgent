"""Subagent ``execution_strategy`` + ``provider_kind`` YAML round-trip (T-S1).

Locks the contract that ``PoolStore.write_pool`` persists a subagent's
``execution_strategy`` and ``provider_kind`` to ``templates/<agent>.yml`` so a
hand-edited or WebUI-saved external subagent survives a read→write cycle
without losing its strategy. Default values (``react`` / ``None``) MUST be
stripped so legacy native subagent templates stay free of
``execution_strategy: react`` noise.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from modex_agent.agents.external.paths import ProviderKind
from modex_agent.core.constants import ExecutionStrategyKind
from modex_agent.multi_agent.pool_config import (
    MainAgentSpec,
    PoolSpec,
    PoolStore,
    SubagentSpec,
)


def _template_path(base: Path, pool: str, agent: str) -> Path:
    return base / "config" / "pools" / pool / "templates" / f"{agent}.yml"


class TestSubagentExecutionStrategyRoundTrip:
    """A subagent's ``execution_strategy`` + ``provider_kind`` round-trip via
    the on-disk YAML template."""

    def test_external_subagent_round_trips(self, tmp_path: Path) -> None:
        store = PoolStore(base_dir=tmp_path)
        tree = PoolSpec(
            name="coding",
            main_agent_name="main",
            main=MainAgentSpec(agent_name="main"),
            subagents=[
                SubagentSpec(
                    agent_name="ext",
                    execution_strategy=ExecutionStrategyKind.EXTERNAL,
                    provider_kind=ProviderKind.OPENCODE,
                ),
            ],
        )
        store.write_pool("coding", tree)

        reread = store.read_pool("coding")
        assert len(reread.subagents) == 1
        ext = reread.subagents[0]
        assert ext.execution_strategy == ExecutionStrategyKind.EXTERNAL
        assert ext.provider_kind == ProviderKind.OPENCODE

        raw_text = _template_path(tmp_path, "coding", "ext").read_text(encoding="utf-8")
        assert "execution_strategy: external" in raw_text
        assert "provider_kind: opencode" in raw_text

    def test_native_react_subagent_round_trips_without_default_noise(self, tmp_path: Path) -> None:
        store = PoolStore(base_dir=tmp_path)
        tree = PoolSpec(
            name="coding",
            main_agent_name="main",
            main=MainAgentSpec(agent_name="main"),
            subagents=[
                SubagentSpec(agent_name="native"),  # defaults: react + None
            ],
        )
        store.write_pool("coding", tree)

        reread = store.read_pool("coding")
        assert len(reread.subagents) == 1
        native = reread.subagents[0]
        assert native.execution_strategy == ExecutionStrategyKind.REACT
        assert native.provider_kind is None

        raw = yaml.safe_load(
            _template_path(tmp_path, "coding", "native").read_text(encoding="utf-8")
        )
        assert "execution_strategy" not in raw
        assert "provider_kind" not in raw

    def test_mixed_subagents_round_trip_together(self, tmp_path: Path) -> None:
        """One external + one native subagent in the same pool: both
        strategies survive and the native template stays free of
        ``execution_strategy:`` lines."""
        store = PoolStore(base_dir=tmp_path)
        tree = PoolSpec(
            name="coding",
            main_agent_name="main",
            main=MainAgentSpec(agent_name="main"),
            subagents=[
                SubagentSpec(
                    agent_name="ext",
                    execution_strategy=ExecutionStrategyKind.EXTERNAL,
                    provider_kind=ProviderKind.OPENCODE,
                ),
                SubagentSpec(agent_name="native"),
            ],
        )
        store.write_pool("coding", tree)

        reread = store.read_pool("coding")
        by_name = {sub.agent_name: sub for sub in reread.subagents}
        assert by_name["ext"].execution_strategy == ExecutionStrategyKind.EXTERNAL
        assert by_name["ext"].provider_kind == ProviderKind.OPENCODE
        assert by_name["native"].execution_strategy == ExecutionStrategyKind.REACT
        assert by_name["native"].provider_kind is None

        ext_text = _template_path(tmp_path, "coding", "ext").read_text(encoding="utf-8")
        assert "execution_strategy: external" in ext_text
        assert "provider_kind: opencode" in ext_text

        native_raw = yaml.safe_load(
            _template_path(tmp_path, "coding", "native").read_text(encoding="utf-8")
        )
        assert "execution_strategy" not in native_raw
        assert "provider_kind" not in native_raw
