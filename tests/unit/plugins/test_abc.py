"""TDD tests for the new plugin-unified agent assembly type hierarchy.

These tests are written FIRST and drive the implementation of
``src/modex_agent/plugins/abc.py`` (task 1 of the scope-converge
implementation plan). They assert the exact type contract: 10 ComponentSlot members,
4 AgentType members, the ComponentFactory/SimpleFactory/HookFactory
hierarchy, and the two HookRunnerKind values.

Rule 7 (no structural interfaces) is enforced by asserting the source
contains no ``Protocol`` substring.
"""
from __future__ import annotations

import inspect
from abc import ABC
from enum import StrEnum
from pathlib import Path

import pytest
from pydantic import BaseModel

from modex_agent.plugins.abc import (
    AgentType,
    ComponentFactory,
    ComponentSlot,
    HookFactory,
    HookRunnerKind,
    MemoryHookFactory,
    ReactHookFactory,
    SimpleFactory,
)


# ---- ComponentSlot (10 members) ----


class TestComponentSlot:
    def test_is_strenum(self) -> None:
        assert issubclass(ComponentSlot, StrEnum)

    def test_has_exactly_10_members(self) -> None:
        members = list(ComponentSlot)
        assert len(members) == 10

    def test_member_names_exact(self) -> None:
        expected = {
            "TOOL",
            "HOOK",
            "MEMORY_SYSTEM",
            "LLM_PROVIDER",
            "SYSTEM_PROMPT_PROVIDER",
            "INTERCEPTOR",
            "COMMAND_HANDLER",
            "EXECUTION_STRATEGY",
            "INPUT_STAGE",
            "DATA_NAMESPACE",
        }
        actual = {m.name for m in ComponentSlot}
        assert actual == expected

    @pytest.mark.parametrize(
        "name",
        [
            "TOOL",
            "HOOK",
            "MEMORY_SYSTEM",
            "LLM_PROVIDER",
            "SYSTEM_PROMPT_PROVIDER",
            "INTERCEPTOR",
            "COMMAND_HANDLER",
            "EXECUTION_STRATEGY",
            "INPUT_STAGE",
            "DATA_NAMESPACE",
        ],
    )
    def test_each_member_accessible_by_name(self, name: str) -> None:
        assert hasattr(ComponentSlot, name)
        member = getattr(ComponentSlot, name)
        assert isinstance(member, ComponentSlot)


# ---- AgentType (4 members) ----


class TestAgentType:
    def test_is_strenum(self) -> None:
        assert issubclass(AgentType, StrEnum)

    def test_has_exactly_4_members(self) -> None:
        members = list(AgentType)
        assert len(members) == 4

    def test_member_names_exact(self) -> None:
        expected = {"native_main", "native_sub", "external_main", "external_sub"}
        actual = {m.name for m in AgentType}
        assert actual == expected


# ---- HookRunnerKind (2 members) ----


class TestHookRunnerKind:
    def test_is_strenum(self) -> None:
        assert issubclass(HookRunnerKind, StrEnum)

    def test_has_exactly_2_members(self) -> None:
        members = list(HookRunnerKind)
        assert len(members) == 2

    def test_member_names_exact(self) -> None:
        expected = {"react", "memory"}
        actual = {m.name for m in HookRunnerKind}
        assert actual == expected

    def test_no_third_value(self) -> None:
        # Hard guard against adding a third HookRunnerKind.
        assert HookRunnerKind.react.value == "react"
        assert HookRunnerKind.memory.value == "memory"


# ---- ComponentFactory (abstract ABC) ----


class TestComponentFactory:
    def test_is_abc(self) -> None:
        assert issubclass(ComponentFactory, ABC)

    def test_config_model_classvar_declared(self) -> None:
        assert "config_model" in ComponentFactory.__annotations__

    def test_create_is_abstract(self) -> None:
        create = ComponentFactory.create
        assert getattr(create, "__isabstractmethod__", False) is True

    def test_create_is_async(self) -> None:
        assert inspect.iscoroutinefunction(ComponentFactory.create)

    def test_cannot_instantiate_directly(self) -> None:
        with pytest.raises(TypeError):
            ComponentFactory()  # type: ignore[abstract]


# ---- SimpleFactory ----


class _DummyConfig(BaseModel):
    """Minimal frozen config model for SimpleFactory tests."""

    model_config = {"frozen": True, "extra": "forbid"}


class TestSimpleFactory:
    def test_is_component_factory_subclass(self) -> None:
        assert issubclass(SimpleFactory, ComponentFactory)

    def test_create_is_async(self) -> None:
        assert inspect.iscoroutinefunction(SimpleFactory.create)

    async def test_create_returns_wrapped_instance(self) -> None:
        marker = object()
        factory = SimpleFactory(instance=marker, config_model=_DummyConfig)
        result = await factory.create(_DummyConfig(), ctx=None)  # type: ignore[arg-type]
        assert result is marker

    async def test_create_ignores_config_and_ctx(self) -> None:
        marker = object()
        factory = SimpleFactory(instance=marker, config_model=_DummyConfig)
        # Pass None for both — SimpleFactory must not depend on them.
        result = await factory.create(None, None)  # type: ignore[arg-type]
        assert result is marker

    def test_config_model_accessible(self) -> None:
        factory = SimpleFactory(instance=object(), config_model=_DummyConfig)
        assert factory.config_model is _DummyConfig


# ---- HookFactory (abstract, extends ComponentFactory) ----


class TestHookFactory:
    def test_is_component_factory_subclass(self) -> None:
        assert issubclass(HookFactory, ComponentFactory)

    def test_applies_to_classvar_default_none(self) -> None:
        assert "applies_to" in HookFactory.__annotations__
        assert HookFactory.applies_to is None

    def test_hook_runner_classvar_declared(self) -> None:
        assert "hook_runner" in HookFactory.__annotations__

    def test_cannot_instantiate_directly(self) -> None:
        with pytest.raises(TypeError):
            HookFactory()  # type: ignore[abstract]


# ---- ReactHookFactory ----


class TestReactHookFactory:
    def test_is_hook_factory_subclass(self) -> None:
        assert issubclass(ReactHookFactory, HookFactory)

    def test_hook_runner_is_react(self) -> None:
        assert ReactHookFactory.hook_runner == HookRunnerKind.react


# ---- MemoryHookFactory ----


class TestMemoryHookFactory:
    def test_is_hook_factory_subclass(self) -> None:
        assert issubclass(MemoryHookFactory, HookFactory)

    def test_hook_runner_is_memory(self) -> None:
        assert MemoryHookFactory.hook_runner == HookRunnerKind.memory


# ---- No structural-interface keyword (rule 7) ----


class TestNoProtocolKeyword:
    """Assert the implementation file contains no ``Protocol`` substring.

    Equivalent to ``grep -c "Protocol" abc.py`` returning 0.
    """

    def test_abc_py_has_no_protocol_substring(self) -> None:
        abc_path = (
            Path(__file__).resolve()
            .parents[3]
            / "src"
            / "modex_agent"
            / "plugins"
            / "abc.py"
        )
        source = abc_path.read_text(encoding="utf-8")
        assert "Protocol" not in source
