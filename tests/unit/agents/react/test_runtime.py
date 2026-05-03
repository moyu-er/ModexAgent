"""Tests for ReActRuntime normalization and clean mode sanitization."""
from framework.agents.react.runtime import ReActRuntime, sanitize_clean_runtime
from framework.core.agent import AgentContext
from framework.core.context_extensions import ExtensionKey
from framework.core.tool_manager import InMemoryToolManager
from framework.hook import HookRunner
from framework.interceptor.chain import InterceptorChain
from framework.memory.history import ListMessageHistory


def make_ctx(**extensions):
    return AgentContext(
        system_prompt="test",
        history=ListMessageHistory(),
        tool_manager=InMemoryToolManager(),
        extensions=extensions,
    )


class TestReActRuntime:
    def test_clean_factory_all_services_none(self):
        rt = ReActRuntime.clean()
        assert rt.mode == "clean"
        assert rt.hooks is None
        assert rt.interceptors is None
        assert rt.approval is None
        assert rt.control is None
        assert rt.checkpoint_store is None
        assert rt.suspend_strategy is None
        assert rt.injection_queue is None
        assert rt.governance is None
        assert rt.safety is None

    def test_from_context_full_mode_preserves_hooks(self):
        ctx = make_ctx(**{"hook_runner": HookRunner()})
        rt = ReActRuntime.from_context(ctx, mode="full")
        assert rt.mode == "full"
        assert rt.hooks is not None

    def test_from_context_clean_mode_disables_all(self):
        ctx = make_ctx(**{
            "hook_runner": HookRunner(),
            "interceptor_chain": InterceptorChain(),
        })
        rt = ReActRuntime.from_context(ctx, mode="clean")
        assert rt.mode == "clean"
        assert rt.hooks is None
        assert rt.interceptors is None

    def test_sanitize_clean_runtime_clears_extension_keys(self):
        ctx = make_ctx(**{
            "hook_runner": HookRunner(),
            "interceptor_chain": InterceptorChain(),
            "checkpoint_store": object(),
        })
        disabled = sanitize_clean_runtime(ctx)
        assert "hook_runner" not in ctx.extensions
        assert "interceptor_chain" not in ctx.extensions
        assert "hook_runner" in disabled

    def test_sanitize_clean_runtime_keeps_non_runtime_keys(self):
        ctx = make_ctx(**{
            ExtensionKey.RUNTIME_CTX_MGR: object(),
            ExtensionKey.MAX_TOOLS_PER_TURN: 5,
        })
        sanitize_clean_runtime(ctx)
        assert ExtensionKey.RUNTIME_CTX_MGR in ctx.extensions
        assert ExtensionKey.MAX_TOOLS_PER_TURN in ctx.extensions
