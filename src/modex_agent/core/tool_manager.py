"""工具管理器 - 抽象基类和实现

提供 ToolManager 抽象层，支持工具注册和执行调度。
"""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from modex_agent.core.capabilities import Modality, ModelCapabilities, ModelInfo
from modex_agent.core.message import ContentFormat, ContentPart, TextPart
from modex_agent.core.tool import DynamicSchemaProvider
from modex_agent.media.store import MediaStore

logger = logging.getLogger(__name__)


# ctx is delivered via contextvar (not as an execute parameter) because MCP
# tools forward **kwargs to the MCP server (tool.py:108 params=kwargs);
# mixing ctx into kwargs would pollute external calls.


class ToolExecutionContext(BaseModel):
    """tool 执行时的只读上下文。

    Frozen BaseModel (rule 10/12). 字段默认全 None → 现有 tool 零改动。
    需要感知模型多模态能力的 tool 通过 :func:`get_tool_execution_context` 读取，
    并用 :meth:`supports` 做声明式能力检查（参考 ADR-0014）。

    声明式能力模型: tool 通过 ``required_modalities`` / ``produced_modalities``
    frozenset 声明所需/所产出的模态；运行时由 :meth:`Tool.is_available` 与
    :meth:`supports` 协同完成可见性过滤与优雅降级。
    """

    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    model_info: ModelInfo | None = None
    workspace_root: Path | None = None
    tool_call_id: str | None = None
    session_id: str | None = None
    media_store: MediaStore | None = None

    def supports(self, modality: Modality) -> bool:
        """Conservative modality check for tools running in this context.

        Returns ``False`` when ``model_info`` is None (no model bound — tools
        should degrade gracefully, matching ``test_read_image_no_ctx_returns_degradation_text``).
        Otherwise delegates to :meth:`ModelCapabilities.supports`.
        """
        if self.model_info is None:
            return False
        return self.model_info.capabilities.supports(modality)


_tool_execution_ctx: ContextVar[ToolExecutionContext | None] = ContextVar(
    "tool_execution_ctx", default=None
)


def get_tool_execution_context() -> ToolExecutionContext | None:
    """Get the current tool execution context, if any.

    Returns ``None`` outside a ``ToolManager.execute`` call.
    """
    return _tool_execution_ctx.get()


@dataclass
class ToolConfig:
    """单个工具的配置"""

    enabled: bool = True  # 是否启用


@dataclass
class ToolManagerConfig:
    """ToolManager 全局配置"""

    pass


class Tool(DynamicSchemaProvider):
    """工具基类

    所有工具应继承此类并实现 execute 方法。

    支持两种使用方式：
    1. 新方式：直接传入参数到 __init__
    2. 旧方式（兼容）：继承后通过 @property 定义 name, description, parameters

    Implements DynamicSchemaProvider — override get_dynamic_schema()
    for context-aware descriptions. Default returns static get_schema().
    """

    required_modalities: frozenset[Modality] = frozenset()
    """Modalities the model MUST support for this tool to be usable.

    Empty (default) = modality-agnostic; the tool is visible to every model.
    Drives :meth:`is_available` filtering in :meth:`ToolManager.get_tool_descriptions`.
    """

    produced_modalities: frozenset[Modality] = frozenset()
    """Modalities this tool may produce in its output.

    Declarative metadata for downstream consumers (e.g. governance, result
    routing). Not used for visibility filtering — a tool that *produces*
    images is still listed for a text-only model (it just degrades at runtime).
    """

    def __init__(
        self,
        name: str | None = None,
        description: str | None = None,
        parameters: dict[str, Any] | None = None,
        config: ToolConfig | None = None,
    ) -> None:
        # 如果子类已经定义了 name/description/parameters 作为属性，则使用它们
        # 否则使用传入的参数
        self._name = name
        self._description = description
        self._parameters = parameters
        self.config = config or ToolConfig()

    @property
    def name(self) -> str:
        """工具名称"""
        if self._name is not None:
            return self._name
        raise NotImplementedError("Tool must define 'name' either via __init__ or as a property")

    @property
    def description(self) -> str:
        """工具描述"""
        if self._description is not None:
            return self._description
        raise NotImplementedError(
            "Tool must define 'description' either via __init__ or as a property"
        )

    @property
    def parameters(self) -> dict[str, Any]:
        """工具参数定义"""
        if self._parameters is not None:
            return self._parameters
        raise NotImplementedError(
            "Tool must define 'parameters' either via __init__ or as a property"
        )

    @abstractmethod
    async def execute(self, **kwargs) -> Any:
        """执行工具

        Args:
            **kwargs: 工具参数

        Returns:
            工具执行结果
        """
        pass

    def get_schema(self) -> dict[str, Any]:
        """获取工具 Schema（供 LLM 使用）

        Returns:
            OpenAI 格式的工具定义
        """
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    def get_dynamic_schema(self) -> dict[str, Any]:
        """DynamicSchemaProvider impl — returns static schema by default.
        Override in subclasses for context-aware descriptions.
        """
        return self.get_schema()

    def is_available(self, caps: ModelCapabilities | None) -> bool:
        """Visibility gate used by :meth:`ToolManager.get_tool_descriptions`.

        Returns ``True`` when ``caps is None`` — don't hide tools when the
        active model's capabilities are unknown (back-compat). Otherwise
        returns ``True`` iff every modality in :attr:`required_modalities`
        is among ``caps.modalities``.
        """
        if caps is None:
            return True
        return self.required_modalities <= caps.modalities

    def result_metadata(self, result: Any) -> tuple[ContentFormat | None, list[str] | None]:
        """Declare content metadata for a tool result, for governance truncation.

        Default: no metadata. Terminal-style tools override to return
        ``(ContentFormat.XML, <truncatable paths>)`` for their XML output.
        """
        return (None, None)


class ToolResult(BaseModel):
    """工具执行结果

    统一的工具执行结果类，兼容所有场景：
    - ToolManager 执行结果
    - Agent 工具调用结果
    - LLM message 格式转换

    Content is the source of truth: ``content: list[ContentPart]`` holds
    TextPart / ImageUrlPart produced by the tool. ``message_content()``
    renders the LLM-facing text (joined TextParts); multimodal consumers
    read the parts from ``content`` directly.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")

    tool_name: str
    error: str | None = None
    execution_time: float = 0.0
    call_id: str | None = None
    overflow_processed: bool = False
    content_format: ContentFormat | None = None
    truncatable_paths: list[str] | None = None
    content: list[ContentPart] = Field(default_factory=list)

    @classmethod
    def from_text(cls, tool_name: str, text: str, **kwargs: Any) -> ToolResult:
        """Build a text-only ToolResult.

        ``text`` is wrapped in a :class:`TextPart` and stored in ``content``.
        """
        return cls(tool_name=tool_name, content=[TextPart(text=text)], **kwargs)

    @property
    def success(self) -> bool:
        """执行是否成功"""
        return self.error is None

    def message_content(self) -> str:
        """Unified LLM-facing content rendering.

        Priority chain:
        1. ``content_format=XML`` with TextParts in ``content`` → render verbatim.
        2. ``content`` has TextParts → join them.
        3. ``error`` → ``"Error: {error}"``.
        4. ``""``.
        """
        text_parts = [p.text for p in self.content if isinstance(p, TextPart)]
        if self.content_format is ContentFormat.XML and text_parts:
            return "".join(text_parts)
        if text_parts:
            return "".join(text_parts)
        if self.error is not None:
            return f"Error: {self.error}"
        return ""

    def __repr__(self) -> str:
        status = "error" if self.error else "success"
        return f"ToolResult({self.tool_name}, {status}, {self.execution_time:.2f}s)"

    def to_dict(self) -> dict[str, Any]:
        """转换为字典"""
        return {
            "tool_name": self.tool_name,
            "error": self.error,
            "execution_time": self.execution_time,
            "call_id": self.call_id,
            "success": self.success,
            "content": [p.model_dump() for p in self.content],
        }

    def to_message(self) -> dict[str, Any]:
        """转换为 LLM message 格式 (OpenAI tool message).

        Terminal-tool results carry content_format/truncatable_paths metadata
        (declared by the tool via result_metadata) for governance truncation.
        """
        from .types import MessageRole

        msg: dict[str, Any] = {
            "role": MessageRole.TOOL.value,
            "tool_call_id": self.call_id or "",
            "name": self.tool_name,
            "content": self.message_content(),
        }
        if self.content_format is not None and self.truncatable_paths is not None:
            msg["content_format"] = self.content_format.value
            msg["truncatable_paths"] = self.truncatable_paths
        return msg


class ToolManager(ABC):
    """工具管理器抽象基类

    职责：
    1. 工具注册/注销（动态扩展）
    2. 工具执行调度
    3. 工具配置管理
    4. 生成工具描述给 LLM

    不处理：
    - 具体的工具实现（由 Tool 子类实现）
    - LLM 调用
    """

    def __init__(self, config: ToolManagerConfig | None = None) -> None:
        self.config = config or ToolManagerConfig()

    # ---- 工具注册/注销 ----

    @abstractmethod
    def register(self, tool: Tool, config: ToolConfig | None = None) -> None:
        """注册工具

        Args:
            tool: 工具实例
            config: 工具特定配置（可选）
        """
        pass

    @abstractmethod
    def unregister(self, tool_name: str) -> bool:
        """注销工具

        Args:
            tool_name: 工具名称

        Returns:
            是否成功注销
        """
        pass

    @abstractmethod
    def get_tool(self, tool_name: str) -> Tool | None:
        """获取工具实例"""
        pass

    @abstractmethod
    def list_tools(self) -> list[str]:
        """列出所有已注册的工具名称"""
        pass

    @abstractmethod
    def is_registered(self, tool_name: str) -> bool:
        """检查工具是否已注册"""
        pass

    # ---- 工具执行 ----

    async def execute(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        ctx: ToolExecutionContext | None = None,
    ) -> ToolResult:
        """执行单个工具

        Args:
            tool_name: 工具名称
            arguments: 工具参数
            ctx: 工具执行上下文（模型能力等），通过 contextvar 交付给工具

        Returns:
            ToolResult: 执行结果
        """
        tool = self.get_tool(tool_name)

        if tool is None:
            available_tools = self.list_tools()
            logger.warning(f"Tool not found: {tool_name}. Available tools: {available_tools}")
            return ToolResult(
                tool_name=tool_name,
                error=f"Tool '{tool_name}' not found. Available: {available_tools}",
            )

        if not tool.config.enabled:
            logger.warning(f"Tool disabled: {tool_name}")
            return ToolResult(
                tool_name=tool_name,
                error=f"Tool '{tool_name}' is disabled",
            )

        start_time = asyncio.get_event_loop().time()
        token = _tool_execution_ctx.set(ctx)
        try:
            result = await tool.execute(**arguments)
            execution_time = asyncio.get_event_loop().time() - start_time
            # Exact-type check (not isinstance): a subclass returned by a tool
            # should not silently bypass the content-copy re-wrap path, which
            # normalizes content_format/truncatable_paths via result_metadata.
            if type(result) is ToolResult:
                content_format, truncatable_paths = tool.result_metadata(result.message_content())
                return ToolResult(
                    tool_name=result.tool_name,
                    error=result.error,
                    execution_time=execution_time,
                    call_id=result.call_id,
                    content_format=content_format,
                    truncatable_paths=truncatable_paths,
                    content=result.content,
                )
            content_format, truncatable_paths = tool.result_metadata(result)
            if isinstance(result, str):
                return ToolResult(
                    tool_name=tool_name,
                    content=[TextPart(text=result)],
                    content_format=content_format,
                    truncatable_paths=truncatable_paths,
                    execution_time=execution_time,
                )
            if result is None:
                return ToolResult(
                    tool_name=tool_name,
                    execution_time=execution_time,
                    content_format=content_format,
                    truncatable_paths=truncatable_paths,
                )
            return ToolResult(
                tool_name=tool_name,
                content=[TextPart(text=str(result))],
                execution_time=execution_time,
                content_format=content_format,
                truncatable_paths=truncatable_paths,
            )
        except Exception as e:
            execution_time = asyncio.get_event_loop().time() - start_time
            return ToolResult(
                tool_name=tool_name,
                error=f"Tool '{tool_name}' execution failed: {str(e)}",
                execution_time=execution_time,
            )
        finally:
            _tool_execution_ctx.reset(token)

    # ---- 工具描述生成 ----

    def get_tool_descriptions(
        self, caps: ModelCapabilities | None = None
    ) -> list[dict[str, Any]]:
        """获取所有工具的描述（供 LLM 使用）

        Args:
            caps: 当前模型的 capabilities。``None`` 时不过滤（back-compat，
                与现有调用方一致）。非 None 时，跳过 :meth:`Tool.is_available`
                返回 False 的工具，并把 ``caps`` 传给
                :meth:`Tool.get_dynamic_schema_for` 以生成能力感知的 schema。

        Returns:
            OpenAI 格式的工具定义列表
        """
        descriptions = []
        for tool_name in self.list_tools():
            tool = self.get_tool(tool_name)
            if tool is None:
                continue
            if tool.config.enabled and tool.is_available(caps):
                descriptions.append(tool.get_dynamic_schema_for(caps))
        return descriptions


class InMemoryToolManager(ToolManager):
    """内存中的工具管理器实现"""

    def __init__(self, config: ToolManagerConfig | None = None) -> None:
        super().__init__(config)
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool, config: ToolConfig | None = None) -> None:
        """注册工具"""
        self._tools[tool.name] = tool
        if config:
            tool.config = config
        logger.info(f"Tool registered: {tool.name}")

    def unregister(self, tool_name: str) -> bool:
        """注销工具"""
        if tool_name in self._tools:
            self._tools.pop(tool_name)
            logger.debug(f"Tool unregistered: {tool_name}")
            return True
        return False

    def get_tool(self, tool_name: str) -> Tool | None:
        """获取工具"""
        return self._tools.get(tool_name)

    def list_tools(self) -> list[str]:
        """列出所有工具"""
        return list(self._tools.keys())

    def is_registered(self, tool_name: str) -> bool:
        """检查工具是否已注册"""
        return tool_name in self._tools

    @property
    def tools(self) -> dict[str, Tool]:
        """所有已注册的工具（按名称索引）。调试用，修改 dict 不影响管理器。"""
        return dict(self._tools)

    def __contains__(self, tool_name: str) -> bool:
        """支持 'tool_name in tool_manager' 语法"""
        return self.is_registered(tool_name)
