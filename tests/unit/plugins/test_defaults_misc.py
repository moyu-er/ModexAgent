"""TDD tests for the 4 default factory modules in plugins/defaults/.

Tests all 4 register_default_* functions.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from modex_agent.plugins.abc import ComponentFactory, ComponentSlot, SimpleFactory
from modex_agent.plugins.loader import PluginRegistrationContext
from modex_agent.plugins.registry import ComponentRegistry


# ---- helpers ---------------------------------------------------------------


def _make_ctx() -> PluginRegistrationContext:
    """Build a PluginRegistrationContext backed by a fresh registry."""
    return PluginRegistrationContext(ComponentRegistry())


def _flush(ctx: PluginRegistrationContext) -> ComponentRegistry:
    """Flush the context's buffer into its registry and return the registry."""
    # __exit__ with no exception flushes.
    ctx.__exit__(None, None, None)
    # The registry is the private attr; access it for test assertions.
    return ctx._registry  # noqa: SLF001


# ---- 1. llm.py -------------------------------------------------------------


class TestRegisterDefaultLLM:
    def test_registers_default_in_llm_provider_slot(self):
        from modex_agent.plugins.defaults.llm import register_default_llm

        ctx = _make_ctx()
        register_default_llm(ctx)
        registry = _flush(ctx)
        factory = registry.resolve(ComponentSlot.LLM_PROVIDER, "default")
        assert isinstance(factory, ComponentFactory)

    def test_factory_config_model_has_path_field(self):
        from modex_agent.plugins.defaults.llm import DefaultLLMProviderConfig

        assert "path" in DefaultLLMProviderConfig.model_fields
        # frozen + extra forbid
        cfg = DefaultLLMProviderConfig(path="model.yml")
        with pytest.raises(Exception):
            cfg.path = "other"  # type: ignore[misc]
        with pytest.raises(ValidationError):
            DefaultLLMProviderConfig(path="x", unknown="y")  # type: ignore[call-arg]


# ---- 2. prompt.py ----------------------------------------------------------


class TestRegisterDefaultPrompts:
    def test_registers_file_prompt_in_system_prompt_slot(self):
        from modex_agent.plugins.defaults.prompt import register_default_prompts

        ctx = _make_ctx()
        register_default_prompts(ctx)
        registry = _flush(ctx)
        factory = registry.resolve(
            ComponentSlot.SYSTEM_PROMPT_PROVIDER, "file_prompt"
        )
        assert isinstance(factory, ComponentFactory)

    def test_factory_config_model_has_path_field(self):
        from modex_agent.plugins.defaults.prompt import FilePromptConfig

        assert "path" in FilePromptConfig.model_fields
        cfg = FilePromptConfig(path="agents/main.md")
        with pytest.raises(Exception):
            cfg.path = "other"  # type: ignore[misc]


# ---- 3. interceptors.py ----------------------------------------------------


class TestRegisterDefaultInterceptors:
    def test_registers_tool_timeout_in_interceptor_slot(self):
        from modex_agent.plugins.defaults.interceptors import (
            register_default_interceptors,
        )

        ctx = _make_ctx()
        register_default_interceptors(ctx)
        registry = _flush(ctx)
        factory = registry.resolve(ComponentSlot.INTERCEPTOR, "tool_timeout")
        assert isinstance(factory, ComponentFactory)

    async def test_factory_creates_tool_timeout_interceptor(self):
        from modex_agent.interceptor.builtin.tool_timeout import (
            ToolTimeoutInterceptor,
        )

        from modex_agent.plugins.defaults.interceptors import (
            ToolTimeoutInterceptorFactory,
        )

        factory = ToolTimeoutInterceptorFactory()
        config = factory.config_model()
        instance = await factory.create(config, ctx=None)  # type: ignore[arg-type]
        assert isinstance(instance, ToolTimeoutInterceptor)


# ---- 5. commands.py --------------------------------------------------------


class TestRegisterDefaultCommands:
    def test_registers_all_6_command_factories(self):
        from modex_agent.plugins.defaults.commands import register_default_commands

        ctx = _make_ctx()
        register_default_commands(ctx)
        registry = _flush(ctx)

        for name in ("cd", "stop", "pool", "approve", "deny", "continue"):
            factory = registry.resolve(ComponentSlot.COMMAND_HANDLER, name)
            assert isinstance(factory, ComponentFactory), f"{name} not registered"

    async def test_approve_factory_creates_approval_handler(self):
        from modex_agent.commands.handlers import ApprovalCommandHandler

        from modex_agent.plugins.defaults.commands import (
            ApproveCommandHandlerFactory,
        )

        factory = ApproveCommandHandlerFactory()
        config = factory.config_model()
        handler = await factory.create(config, ctx=None)  # type: ignore[arg-type]
        assert isinstance(handler, ApprovalCommandHandler)
        assert "approve" in handler.names

    async def test_deny_factory_creates_approval_handler(self):
        from modex_agent.commands.handlers import ApprovalCommandHandler

        from modex_agent.plugins.defaults.commands import (
            DenyCommandHandlerFactory,
        )

        factory = DenyCommandHandlerFactory()
        config = factory.config_model()
        handler = await factory.create(config, ctx=None)  # type: ignore[arg-type]
        assert isinstance(handler, ApprovalCommandHandler)
        assert "deny" in handler.names

    async def test_continue_factory_creates_continue_handler(self):
        from modex_agent.commands.handlers import ContinueCommandHandler

        from modex_agent.plugins.defaults.commands import (
            ContinueCommandHandlerFactory,
        )

        factory = ContinueCommandHandlerFactory()
        config = factory.config_model()
        handler = await factory.create(config, ctx=None)  # type: ignore[arg-type]
        assert isinstance(handler, ContinueCommandHandler)

    async def test_stop_factory_creates_control_handler(self):
        from modex_agent.commands.handlers import ControlCommandHandler

        from modex_agent.plugins.defaults.commands import (
            StopCommandHandlerFactory,
        )

        factory = StopCommandHandlerFactory()
        config = factory.config_model()
        handler = await factory.create(config, ctx=None)  # type: ignore[arg-type]
        assert isinstance(handler, ControlCommandHandler)
        assert "stop" in handler.names

    async def test_cd_factory_creates_handler(self):
        from modex_agent.commands.handlers import CommandHandler

        from modex_agent.plugins.defaults.commands import (
            CdCommandHandlerFactory,
        )

        factory = CdCommandHandlerFactory()
        config = factory.config_model()
        handler = await factory.create(config, ctx=None)  # type: ignore[arg-type]
        assert isinstance(handler, CommandHandler)
        assert "cd" in handler.names

    async def test_pool_factory_creates_handler(self):
        from modex_agent.commands.handlers import CommandHandler

        from modex_agent.plugins.defaults.commands import (
            PoolCommandHandlerFactory,
        )

        factory = PoolCommandHandlerFactory()
        config = factory.config_model()
        handler = await factory.create(config, ctx=None)  # type: ignore[arg-type]
        assert isinstance(handler, CommandHandler)
        assert "pool" in handler.names


# ---- all 4 modules import without error ------------------------------------


class TestModuleImports:
    def test_all_4_modules_importable(self):
        import modex_agent.plugins.defaults.commands  # noqa: F401
        import modex_agent.plugins.defaults.interceptors  # noqa: F401
        import modex_agent.plugins.defaults.llm  # noqa: F401
        import modex_agent.plugins.defaults.prompt  # noqa: F401
