from __future__ import annotations

import pytest
from pydantic import ValidationError

from modex_agent.core.tool_group import (
    ToolGroup,
    ToolGroupResource,
    ToolGroupSpec,
    ToolGroupVariant,
)
from modex_agent.core.tool_manager import Tool


class _Tool(Tool):
    def __init__(self, name: str) -> None:
        super().__init__(name=name, description=name, parameters={"type": "object"})

    async def execute(self, **kwargs: object) -> str:
        return self.name


class _Resource(ToolGroupResource):
    async def aclose(self) -> None:
        return None


def test_tool_group_contract_keeps_variant_tools_and_resource() -> None:
    resource = _Resource()
    tools = (_Tool("bash"), _Tool("bash_input"))

    group = ToolGroup(
        anchor="bash",
        variant="persistent",
        tools=tools,
        resource=resource,
    )

    assert group.anchor == "bash"
    assert group.variant == "persistent"
    assert group.tools == tools
    assert group.resource is resource


def test_tool_group_runtime_metadata_is_read_only() -> None:
    group = ToolGroup(
        anchor="bash",
        variant="persistent",
        tools=(_Tool("bash"), _Tool("bash_input")),
    )

    with pytest.raises(AttributeError):
        group.anchor = "other"  # type: ignore[misc]
    with pytest.raises(AttributeError):
        group.variant = "other"  # type: ignore[misc]
    with pytest.raises(AttributeError):
        group.tools = ()  # type: ignore[misc]
    with pytest.raises(AttributeError):
        group.extra = True  # type: ignore[attr-defined]


def test_tool_group_spec_is_frozen_and_rejects_unknown_fields() -> None:
    spec = ToolGroupSpec(
        anchor="bash",
        variants=(ToolGroupVariant(name="persistent", tools=("bash", "bash_input")),),
    )

    with pytest.raises(ValidationError):
        ToolGroupSpec.model_validate(
            {
                **spec.model_dump(),
                "unexpected": True,
            }
        )
    with pytest.raises(ValidationError):
        spec.anchor = "other"  # type: ignore[misc]
