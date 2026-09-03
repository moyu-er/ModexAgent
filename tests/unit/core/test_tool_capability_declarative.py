"""Declarative tool-capability awareness tests (ADR-0014 §4).

Covers ``Tool.required_modalities`` / ``produced_modalities`` class attrs,
``Tool.is_available``, ``ToolExecutionContext.supports``, the caps-aware
``ToolManager.get_tool_descriptions`` filtering, and
``get_dynamic_schema_for`` description adjustment — including delegation
through ``WorkspaceScopedTool``.

Lineage: ADR-0013 (attachment system) and ADR-0014 (mechanism A activation)
solved the attachment/inline side; this module tests the tool-side declarative
awareness that fills the gap they left open.
"""

from __future__ import annotations

from pathlib import Path

from modex_agent.core.capabilities import Modality, ModelCapabilities, ModelInfo
from modex_agent.core.tool_manager import (
    Tool,
    ToolExecutionContext,
    ToolResult,
)
from modex_agent.tools.manager import InMemoryToolManager
from modex_agent.tools.standard.file_tool import ReadFileTool
from modex_agent.tools.workspace_scoped import (
    WorkspaceRootProvider,
    WorkspaceScopedFileTool,
)

# -- fixtures -----------------------------------------------------------------

_CAPABLE = ModelInfo(
    model_name="test-vision",
    capabilities=ModelCapabilities(modalities=frozenset({Modality.TEXT, Modality.IMAGE})),
)
_TEXT_ONLY = ModelInfo(
    model_name="test-text",
    capabilities=ModelCapabilities(modalities=frozenset({Modality.TEXT})),
)


class _PlainTool(Tool):
    """Modality-agnostic tool (empty ``required_modalities``)."""

    def __init__(self) -> None:
        super().__init__(
            name="plain",
            description="a plain tool",
            parameters={"type": "object", "properties": {}},
        )

    async def execute(self, **kwargs) -> ToolResult:
        return ToolResult.from_text(self.name, "ok")


class _ImageRequiredTool(Tool):
    """Tool that requires IMAGE — must be hidden from text-only models."""

    required_modalities = frozenset({Modality.IMAGE})

    def __init__(self) -> None:
        super().__init__(
            name="image_required",
            description="needs image capability",
            parameters={"type": "object", "properties": {}},
        )

    async def execute(self, **kwargs) -> ToolResult:
        return ToolResult.from_text(self.name, "ok")


class _StaticRootProvider(WorkspaceRootProvider):
    """Returns a fixed workspace root for wrapper tests."""

    def __init__(self, root: Path) -> None:
        self._root = root

    def current(self) -> Path:
        return self._root


class TestDeclarativeToolCapability:
    """ADR-0014: tools declare modality needs; the runtime filters/adjusts."""

    # -- Tool.is_available ----------------------------------------------------

    def test_is_available_none_caps_returns_true(self):
        """``is_available(None)`` is always True (unknown model -> back-compat)."""
        assert _PlainTool().is_available(None) is True

    def test_is_available_empty_required_with_caps_returns_true(self):
        """Empty ``required_modalities`` -> visible to every model."""
        assert _PlainTool().is_available(_TEXT_ONLY.capabilities) is True

    def test_is_available_image_required_text_only_returns_false(self):
        """``required={IMAGE}`` is hidden from a text-only model."""
        assert _ImageRequiredTool().is_available(_TEXT_ONLY.capabilities) is False

    # -- ToolExecutionContext.supports ----------------------------------------

    def test_supports_image_false_when_no_model_info(self):
        """``ToolExecutionContext()`` with None model_info -> supports() False."""
        ctx = ToolExecutionContext()
        assert ctx.supports(Modality.IMAGE) is False

    def test_supports_image_true_when_capable_model_info(self):
        """Capable model_info -> supports(IMAGE) True."""
        ctx = ToolExecutionContext(model_info=_CAPABLE)
        assert ctx.supports(Modality.IMAGE) is True

    def test_supports_image_false_when_text_only_model_info(self):
        """Text-only model_info -> supports(IMAGE) False."""
        ctx = ToolExecutionContext(model_info=_TEXT_ONLY)
        assert ctx.supports(Modality.IMAGE) is False

    # -- ToolManager.get_tool_descriptions (filter + adjust) ------------------

    def test_get_tool_descriptions_hides_required_keeps_produced(self):
        """Text-only caps: ``required={IMAGE}`` tool hidden, ``produced={IMAGE}``
        tool (ReadFileTool) kept and shown with an adjusted description."""
        mgr = InMemoryToolManager()
        mgr.register(_ImageRequiredTool())
        mgr.register(ReadFileTool())

        descs = mgr.get_tool_descriptions(_TEXT_ONLY.capabilities)
        names = [d["function"]["name"] for d in descs]

        assert "image_required" not in names
        assert "read" in names

        read_desc = next(
            d["function"]["description"] for d in descs if d["function"]["name"] == "read"
        )
        assert "cannot be read" in read_desc

    # -- ReadFileTool.get_dynamic_schema_for (caps-aware description) ---------

    def test_readfile_dynamic_schema_text_only_says_cannot_be_read(self):
        """``ReadFileTool.get_dynamic_schema_for(text_only)`` -> 'cannot be read'."""
        schema = ReadFileTool().get_dynamic_schema_for(_TEXT_ONLY.capabilities)
        assert "cannot be read" in schema["function"]["description"]

    def test_readfile_dynamic_schema_capable_says_may_also_be_read(self):
        """``ReadFileTool.get_dynamic_schema_for(capable)`` -> 'may also be read'."""
        schema = ReadFileTool().get_dynamic_schema_for(_CAPABLE.capabilities)
        assert "may also be read" in schema["function"]["description"]

    # -- WorkspaceScopedFileTool delegates get_dynamic_schema_for -------------

    def test_workspace_scoped_file_tool_delegates_dynamic_schema(self):
        """``WorkspaceScopedFileTool.get_dynamic_schema_for`` reaches the inner
        ``ReadFileTool``'s caps-aware override: text-only -> 'cannot be read',
        capable -> 'may also be read'."""
        inner = ReadFileTool()
        wrapper = WorkspaceScopedFileTool(inner, _StaticRootProvider(Path("/tmp")))

        text_desc = wrapper.get_dynamic_schema_for(_TEXT_ONLY.capabilities)[
            "function"
        ]["description"]
        assert "cannot be read" in text_desc

        capable_desc = wrapper.get_dynamic_schema_for(_CAPABLE.capabilities)[
            "function"
        ]["description"]
        assert "may also be read" in capable_desc
