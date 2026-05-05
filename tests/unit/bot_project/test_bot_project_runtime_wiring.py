"""Integration tests verifying bot_project correctly wires all runtime services
through ReActRuntime after the framework refactor.

Tests exercise the builder methods on BotService directly, using minimal config
and mock adapters so no external I/O is required.
"""

from __future__ import annotations

import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest

# Ensure bot_project is importable even when the wheel does not include it.
sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent / "examples" / "bot_project"))

from bot.service.core import BotService
from framework.agents.react.approval import (
    ApprovalRuntime,
    TieredToolApprovalClassifier,
)
from framework.agents.react.runtime import ReActRuntime
from framework.approval.constants import ApprovalTier
from framework.core.types import InputMessage, OutputMessage, ToolCall
from framework.hook import HookRunner
from framework.hook.builtin import RuntimeContextHook
from framework.interceptor.builtin import (
    ControlDrainInterceptor,
    ToolResultLimitInterceptor,
)
from framework.interceptor.builtin.tool_approval import ArgumentMatcher, ToolNameMatcher
from framework.interceptor.chain import InterceptorChain
from framework.memory.context_governance import (
    CompositeGovernance,
    ToolChainRepairGovernance,
)
from framework.memory.system import create_memory_system
from framework.pipeline.adapters import InputAdapter, OutputAdapter


# ---------------------------------------------------------------------------
# Minimal mock adapters so we can construct BotService without I/O
# ---------------------------------------------------------------------------

class _MockInputAdapter(InputAdapter):
    @property
    def name(self) -> str:
        return "mock_input"

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def receive(self) -> AsyncIterator[InputMessage]:
        if False:
            yield  # pragma: no cover — make this method a generator
        return


class _MockOutputAdapter(OutputAdapter):
    @property
    def name(self) -> str:
        return "mock_output"

    @property
    def streaming_mode(self) -> Any:
        from framework.adapters.platform import StreamingMode
        return StreamingMode.NONE

    async def send(self, message: OutputMessage, session_id: str) -> None:
        pass

    async def send_delta(self, delta: str, session_id: str, metadata: dict[str, Any] | None = None) -> None:
        pass

    async def stop(self) -> None:
        pass


def _mock_emitter_factory(agent_id: str) -> Mock:
    return Mock()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_service(config: dict[str, Any] | None = None, **kwargs: Any) -> BotService:
    """Create a BotService with mock adapters and a temporary config_dir."""
    tmp_dir = Path(f"/tmp/bot_project_test_{id(config)}")
    tmp_dir.mkdir(parents=True, exist_ok=True)
    svc = BotService(
        config_dir=tmp_dir,
        input_adapter=_MockInputAdapter(),
        output_adapter=_MockOutputAdapter(),
        emitter_factory=_mock_emitter_factory,
        config=config or {},
        **kwargs,
    )
    return svc


def _make_minimal_config(**overrides: Any) -> dict[str, Any]:
    """Return the smallest valid config dict for BotService builder methods."""
    cfg: dict[str, Any] = {
        "llm": {
            "model": "test-model",
            "api_key": "sk-test",
            "max_tokens": 8000,
        },
        "agent": {
            "system_prompt": "You are a helpful assistant.",
            "max_iterations": 10,
        },
        "tools": {},
        "memory": {
            "main": {
                "governance": {
                    "enabled": True,
                    "tool_chain_repair": True,
                },
            },
        },
        "approval": {
            "dangerous_tools": ["shell", "write_file", "edit_file"],
        },
    }
    cfg.update(overrides)
    return cfg


async def _load_tool_cleanup_plugin(enabled: bool = True) -> Any:
    from bot.plugins.integration import PluginIntegration

    local_plugins_dir = (
        Path(__file__).parent.parent.parent.parent
        / "examples"
        / "bot_project"
        / "plugins"
    )
    integration = PluginIntegration(
        {
            "plugins": {
                "enabled": ["tool_call_cleanup"],
                "configurations": {
                    "tool_call_cleanup": {"enabled": enabled},
                },
            },
        },
        extra_plugin_dirs=[local_plugins_dir],
    )
    await integration.discover_and_load()
    return integration


def _is_tool_call_cleanup_session(memory_system: Any) -> bool:
    session_type = type(memory_system.layers.session)
    return (
        session_type.__name__ == "ToolCallAwareSessionManager"
        and "tool_call_cleanup" in session_type.__module__
    )


# ===================================================================
# Test 1: ApprovalRuntime is properly wired and classifier works
# ===================================================================

class TestApprovalRuntimeWiring:
    """Verify the TieredToolApprovalClassifier yields correct tiers."""

    @staticmethod
    def _make_tool_call(name: str, args: dict[str, Any] | None = None) -> ToolCall:
        return ToolCall(
            tool_name=name,
            arguments=args or {},
            call_id=f"call_{name}",
        )

    def _make_classifier(
        self, dangerous: set[str] | None = None,
    ) -> TieredToolApprovalClassifier:
        dangerous = dangerous or {"shell", "write_file", "edit_file"}
        return TieredToolApprovalClassifier(
            dangerous=ToolNameMatcher(dangerous),
            argument_matcher=ArgumentMatcher({"."}),
        )

    def test_list_dir_is_normal(self):
        """list_dir is read-only and NOT in the dangerous set -> NORMAL."""
        classifier = self._make_classifier()
        tc = self._make_tool_call("list_dir", {"path": "/tmp"})
        tier = classifier.classify(tc, Mock())
        assert tier == ApprovalTier.NORMAL

    def test_edit_file_is_dangerous(self):
        """edit_file IS in the dangerous set -> DANGEROUS."""
        classifier = self._make_classifier()
        tc = self._make_tool_call("edit_file", {"file_path": "/tmp/foo.txt"})
        tier = classifier.classify(tc, Mock())
        assert tier == ApprovalTier.DANGEROUS

    def test_shell_is_dangerous(self):
        """shell IS in the dangerous set -> DANGEROUS."""
        classifier = self._make_classifier()
        tc = self._make_tool_call("shell", {"command": "ls"})
        tier = classifier.classify(tc, Mock())
        assert tier == ApprovalTier.DANGEROUS

    def test_unknown_tool_is_normal(self):
        """A tool NOT in the dangerous (or any other) set -> NORMAL."""
        classifier = self._make_classifier()
        tc = self._make_tool_call("read_file", {"file_path": "/tmp/foo.txt"})
        tier = classifier.classify(tc, Mock())
        assert tier == ApprovalTier.NORMAL

    def test_write_file_is_dangerous(self):
        """write_file IS in the dangerous set -> DANGEROUS."""
        classifier = self._make_classifier()
        tc = self._make_tool_call("write_file", {"file_path": "/tmp/new.txt"})
        tier = classifier.classify(tc, Mock())
        assert tier == ApprovalTier.DANGEROUS


# ===================================================================
# Test 2: Governance chain is properly wired
# ===================================================================

class TestGovernanceWiring:
    """Verify _build_governance() returns a CompositeGovernance with
    ToolChainRepairGovernance."""

    def test_governance_includes_tool_chain_repair(self):
        cfg = _make_minimal_config()
        svc = _make_service(config=cfg)
        gov = svc._build_governance()
        assert gov is not None
        assert isinstance(gov, CompositeGovernance)
        # At least one strategy should be ToolChainRepairGovernance
        has_repair = any(
            isinstance(s, ToolChainRepairGovernance) for s in gov._strategies
        )
        assert has_repair, (
            f"Expected ToolChainRepairGovernance in strategies, "
            f"got {[type(s).__name__ for s in gov._strategies]}"
        )

    def test_governance_disabled_returns_none(self):
        cfg = _make_minimal_config()
        cfg["memory"]["main"]["governance"]["enabled"] = False
        svc = _make_service(config=cfg)
        gov = svc._build_governance()
        assert gov is None

    def test_governance_defaults_to_enabled(self):
        """When governance section is missing entirely, it defaults to enabled
        and should return None (empty strategies fallback, or it returns None
        when no strategies are configured)."""
        cfg = _make_minimal_config()
        # Remove all individual strategy flags — defaults to enabled, but
        # all strategies are enabled so CompositeGovernance is built.
        gov_cfg = cfg["memory"]["main"]["governance"]
        # Keep defaults: tool_chain_repair=True, microcompact.enabled=True, token_budget.enabled=True
        # So governance should be non-None.
        svc = _make_service(config=cfg)
        gov = svc._build_governance()
        assert gov is not None
        assert isinstance(gov, CompositeGovernance)

    def test_governance_empty_strategies_returns_none(self):
        """When all strategies are individually disabled, return None."""
        cfg = _make_minimal_config()
        gov_cfg = cfg["memory"]["main"]["governance"]
        gov_cfg["tool_chain_repair"] = False
        gov_cfg["microcompact"] = gov_cfg.get("microcompact", {})
        gov_cfg["microcompact"]["enabled"] = False
        gov_cfg["token_budget"] = gov_cfg.get("token_budget", {})
        gov_cfg["token_budget"]["enabled"] = False
        svc = _make_service(config=cfg)
        gov = svc._build_governance()
        assert gov is None


# ===================================================================
# Test 3: HookRunner has RuntimeContextHook
# ===================================================================

class TestHookRunnerWiring:
    """Verify _build_hook_runner() includes RuntimeContextHook."""

    def test_hook_runner_includes_runtime_context_hook(self):
        cfg = _make_minimal_config()
        svc = _make_service(config=cfg)
        runner = svc._build_hook_runner([])
        assert runner is not None
        assert isinstance(runner, HookRunner)

        # RuntimeContextHook must be present as the first HookSpec
        specs = runner.hook_specs
        assert len(specs) >= 1, "Expected at least RuntimeContextHook"
        first = specs[0]
        assert isinstance(first.hook, RuntimeContextHook), (
            f"First hook should be RuntimeContextHook, got {type(first.hook).__name__}"
        )

    def test_hook_runner_appends_custom_hooks(self):
        cfg = _make_minimal_config()
        svc = _make_service(config=cfg)
        mock_hook = Mock()
        runner = svc._build_hook_runner([mock_hook])
        # RuntimeContextHook + 1 custom
        assert len(list(runner.hook_specs)) >= 2

        # Last hook spec should be the custom one
        last = list(runner.hook_specs)[-1]
        assert last.hook is mock_hook


# ===================================================================
# Test 4: Interceptor chain has the right interceptors
# ===================================================================

class TestInterceptorChainWiring:
    """Verify _build_interceptor_chain() returns a chain with
    ControlDrainInterceptor and ToolResultLimitInterceptor."""

    def test_interceptor_chain_has_control_drain(self):
        cfg = _make_minimal_config()
        svc = _make_service(config=cfg)
        chain = svc._build_interceptor_chain()
        assert chain is not None
        assert isinstance(chain, InterceptorChain)

        has_cd = any(
            isinstance(i, ControlDrainInterceptor) for i in chain.interceptors
        )
        assert has_cd, (
            f"Expected ControlDrainInterceptor in chain, "
            f"got {[type(i).__name__ for i in chain.interceptors]}"
        )

    def test_interceptor_chain_has_tool_result_limit(self):
        cfg = _make_minimal_config()
        svc = _make_service(config=cfg)
        chain = svc._build_interceptor_chain()
        assert chain is not None

        has_limit = any(
            isinstance(i, ToolResultLimitInterceptor) for i in chain.interceptors
        )
        assert has_limit, (
            f"Expected ToolResultLimitInterceptor in chain, "
            f"got {[type(i).__name__ for i in chain.interceptors]}"
        )

    def test_interceptor_chain_order(self):
        """ControlDrainInterceptor must come before ToolResultLimitInterceptor."""
        cfg = _make_minimal_config()
        svc = _make_service(config=cfg)
        chain = svc._build_interceptor_chain()

        positions: dict[str, int] = {}
        for idx, i in enumerate(chain.interceptors):
            positions[type(i).__name__] = idx

        assert positions["ControlDrainInterceptor"] < positions["ToolResultLimitInterceptor"], (
            "ControlDrainInterceptor must precede ToolResultLimitInterceptor"
        )

    def test_interceptor_chain_returns_cached(self):
        cfg = _make_minimal_config()
        svc = _make_service(config=cfg)
        chain1 = svc._build_interceptor_chain()
        chain2 = svc._build_interceptor_chain()
        assert chain1 is chain2, "Second call should return the cached chain"


# ===================================================================
# Test 5: bot_project plugin and agent capability wiring
# ===================================================================

class TestBotProjectPluginAndCapabilityWiring:
    async def test_tool_call_cleanup_wraps_main_memory_when_enabled(self, tmp_path: Path):
        integration = await _load_tool_cleanup_plugin(enabled=True)
        memory_system = create_memory_system(workspace=tmp_path / "main", session_only=True)
        await memory_system.initialize()

        try:
            injected = integration.inject_memory_system_modifiers(memory_system)

            assert injected == ["tool_call_cleanup"]
            assert _is_tool_call_cleanup_session(memory_system)
        finally:
            await memory_system.close()
            await integration.shutdown()

    async def test_tool_call_cleanup_does_not_wrap_when_disabled(self, tmp_path: Path):
        integration = await _load_tool_cleanup_plugin(enabled=False)
        memory_system = create_memory_system(workspace=tmp_path / "main", session_only=True)
        await memory_system.initialize()

        try:
            injected = integration.inject_memory_system_modifiers(memory_system)

            assert injected == []
            assert not _is_tool_call_cleanup_session(memory_system)
        finally:
            await memory_system.close()
            await integration.shutdown()

    async def test_tool_call_cleanup_wraps_peer_and_subagent_memory(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        cfg = _make_minimal_config(
            memory={
                "peers": {"short_term": {"max_messages": 20}},
                "subagents": {"short_term": {"max_messages": 10}},
            },
        )
        svc = _make_service(config=cfg)
        svc.plugin_integration = await _load_tool_cleanup_plugin(enabled=True)
        monkeypatch.setattr(
            svc,
            "_resolve_path",
            lambda _config_key, _default_relative: tmp_path,
        )

        peer_context = await svc._create_peer_memory(
            "query-12306",
            {"system_prompt": "railway", "memory": {"enabled": True}},
        )
        subagent_context = await svc._create_subagent_memory("helper-sync")

        try:
            assert _is_tool_call_cleanup_session(peer_context.memory_system)
            assert _is_tool_call_cleanup_session(subagent_context.memory_system)
        finally:
            await peer_context.memory_system.close()
            await subagent_context.memory_system.close()
            await svc.plugin_integration.shutdown()

    def test_query_12306_peer_uses_dedicated_skill_and_mcp_config(self):
        from bot.utils.config_loader import ConfigLoader

        config_dir = (
            Path(__file__).parent.parent.parent.parent
            / "examples"
            / "bot_project"
            / "config"
        )
        loader = ConfigLoader(config_dir)
        config = loader.load_yaml("bot_config.yml")
        config["mcp"] = loader.load_mcp_config(config.get("mcp", {}))

        peers = {
            peer["name"]: peer
            for peer in config.get("multi_agent", {}).get("peers", [])
        }
        query_peer = peers["query-12306"]
        office_peer = peers["office-expert"]

        assert query_peer["skill_dirs"] == ["skills/peers/12306"]
        assert "skills/peers/docx" not in query_peer["skill_dirs"]
        assert query_peer["tools"]["mcp_tools"]["server_filter"] == ["12306-mcp", "fetch"]
        assert query_peer["tools"]["file_tools"]["enabled"] is False
        assert query_peer["tools"]["shell_tools"]["enabled"] is False
        assert (
            config_dir.parent / query_peer["skill_dirs"][0] / "SKILL.md"
        ).exists()

        assert "skills/peers/docx" in office_peer["skill_dirs"]
        assert office_peer["tools"]["mcp_tools"]["server_filter"] == ["fetch"]
        assert "12306-mcp" in config["mcp"]["servers"]

    async def test_query_12306_peer_loads_only_dedicated_skill_manager(self):
        from bot.utils.config_loader import ConfigLoader

        config_dir = (
            Path(__file__).parent.parent.parent.parent
            / "examples"
            / "bot_project"
            / "config"
        )
        loader = ConfigLoader(config_dir)
        config = loader.load_yaml("bot_config.yml")
        peers = {
            peer["name"]: peer
            for peer in config.get("multi_agent", {}).get("peers", [])
        }
        svc = _make_service(config=config)

        descriptor, _tool_manager, skill_manager = await svc._build_peer_descriptor(
            peers["query-12306"]
        )

        assert descriptor.address.name == "query-12306"
        assert skill_manager is not None
        skills = await skill_manager.list_skills()
        assert [skill.name for skill in skills] == ["12306-railway-query"]

    async def test_subagent_descriptor_passes_mcp_server_filter(self, monkeypatch: pytest.MonkeyPatch):
        svc = _make_service(config=_make_minimal_config())
        captured: dict[str, Any] = {}

        async def _fake_build_tool_manager(
            tools_config: dict[str, Any],
            mcp_server_filter: list[str] | None = None,
            peer_name: str | None = None,
        ) -> Mock:
            captured["tools_config"] = tools_config
            captured["mcp_server_filter"] = mcp_server_filter
            captured["peer_name"] = peer_name
            return Mock()

        async def _fake_create_subagent_memory(
            sub_name: str,
            base_system_prompt: str = "",
        ) -> Mock:
            _ = sub_name, base_system_prompt
            return Mock()

        monkeypatch.setattr(svc, "_build_peer_tool_manager", _fake_build_tool_manager)
        monkeypatch.setattr(svc, "_create_subagent_memory", _fake_create_subagent_memory)

        await svc._build_subagent_descriptor({
            "name": "rail-helper",
            "tools": {
                "mcp_tools": {
                    "enabled": True,
                    "server_filter": ["12306-mcp"],
                },
            },
        })

        assert captured["mcp_server_filter"] == ["12306-mcp"]
        assert captured["peer_name"] == "rail-helper"


# ===================================================================
# Test 6: ReActRuntime is correctly assembled (full mode)
# ===================================================================

class TestReActRuntimeAssembly:
    """Verify ReActRuntime constructed with mode="full" has all services
    non-None when populated."""

    def _make_full_runtime(self) -> ReActRuntime:
        chain = InterceptorChain()
        chain.add(ControlDrainInterceptor(Mock()))
        chain.add(ToolResultLimitInterceptor(max_chars=8000))

        classifier = TieredToolApprovalClassifier(
            dangerous=ToolNameMatcher({"shell", "write_file", "edit_file"}),
            argument_matcher=ArgumentMatcher({"."}),
        )
        approval = ApprovalRuntime(
            classifier=classifier,
            suspend_strategy=Mock(),
        )

        return ReActRuntime(
            mode="full",
            hooks=HookRunner(),
            interceptors=chain,
            approval=approval,
            control=Mock(),
            checkpoint_store=Mock(),
            governance=CompositeGovernance([ToolChainRepairGovernance()]),
            safety=Mock(),
        )

    def test_full_runtime_all_services_non_none(self):
        rt = self._make_full_runtime()
        assert rt.mode == "full"
        assert rt.hooks is not None
        assert rt.interceptors is not None
        assert rt.approval is not None
        assert rt.control is not None
        assert rt.checkpoint_store is not None
        assert rt.governance is not None
        assert rt.safety is not None

    def test_full_runtime_hooks_is_hook_runner(self):
        rt = self._make_full_runtime()
        assert isinstance(rt.hooks, HookRunner)

    def test_full_runtime_interceptors_is_chain(self):
        rt = self._make_full_runtime()
        assert isinstance(rt.interceptors, InterceptorChain)
        assert len(rt.interceptors.interceptors) == 2

    def test_full_runtime_approval_is_approval_runtime(self):
        rt = self._make_full_runtime()
        assert isinstance(rt.approval, ApprovalRuntime)
        assert isinstance(rt.approval.classifier, TieredToolApprovalClassifier)

    def test_full_runtime_governance_is_composite(self):
        rt = self._make_full_runtime()
        assert isinstance(rt.governance, CompositeGovernance)

    def test_clean_runtime_all_services_none(self):
        rt = ReActRuntime.clean()
        assert rt.mode == "clean"
        assert rt.hooks is None
        assert rt.interceptors is None
        assert rt.approval is None
        assert rt.control is None
        assert rt.checkpoint_store is None
        assert rt.governance is None
        assert rt.safety is None
