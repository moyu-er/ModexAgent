"""工具管理器 - 抽象基类和实现

提供 ToolManager 抽象层，支持工具注册和执行调度。
"""

import asyncio
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from framework.core.tool import DynamicSchemaProvider

logger = logging.getLogger(__name__)


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


class ToolResult:
    """工具执行结果

    统一的工具执行结果类，兼容所有场景：
    - ToolManager 执行结果
    - Agent 工具调用结果
    - LLM message 格式转换
    """

    def __init__(
        self,
        tool_name: str,
        result: Any = None,
        error: str | None = None,
        execution_time: float = 0.0,
        call_id: str | None = None,
        overflow_processed: bool = False,
    ) -> None:
        self.tool_name = tool_name
        self.result = result
        self.error = error
        self.execution_time = execution_time
        self.call_id = call_id
        # Internal flag — prevents double-processing by overflow interceptors.
        # Not included in to_dict() / to_message() as it is ephemeral.
        self.overflow_processed = overflow_processed

    @property
    def success(self) -> bool:
        """执行是否成功"""
        return self.error is None

    def __repr__(self) -> str:
        status = "error" if self.error else "success"
        return f"ToolResult({self.tool_name}, {status}, {self.execution_time:.2f}s)"

    def to_dict(self) -> dict[str, Any]:
        """转换为字典"""
        return {
            "tool_name": self.tool_name,
            "result": self.result,
            "error": self.error,
            "execution_time": self.execution_time,
            "call_id": self.call_id,
            "success": self.success,
        }

    def to_message(self) -> dict[str, Any]:
        """转换为 LLM message 格式

        Returns:
            OpenAI 格式的 tool message。终端工具的 XML 格式会附加
            content_format 和 truncatable_paths 元数据，供治理层截断。
        """
        from .types import MessageRole

        content = self.result if self.success else f"Error: {self.error}"
        content_str = str(content) if content is not None else ""
        msg: dict[str, Any] = {
            "role": MessageRole.TOOL.value,
            "tool_call_id": self.call_id or "",
            "name": self.tool_name,
            "content": content_str,
        }
        # Detect terminal tool XML and declare truncation metadata
        try:
            from framework.tools.terminal.types import get_terminal_xml_truncatable_paths
        except ImportError:
            return msg
        paths = get_terminal_xml_truncatable_paths(content_str)
        if paths is not None:
            from framework.core.message import ContentFormat

            msg["content_format"] = ContentFormat.XML.value
            msg["truncatable_paths"] = paths
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
    ) -> ToolResult:
        """执行单个工具

        Args:
            tool_name: 工具名称
            arguments: 工具参数

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
        try:
            result = await tool.execute(**arguments)
            execution_time = asyncio.get_event_loop().time() - start_time
            # If the tool already returned a ToolResult (e.g. scoped tools
            # that validate paths and return errors), pass it through so
            # error information reaches the model intact.
            if type(result) is ToolResult:
                return ToolResult(
                    tool_name=result.tool_name,
                    result=result.result,
                    error=result.error,
                    execution_time=execution_time,
                    call_id=result.call_id,
                )
            return ToolResult(
                tool_name=tool_name,
                result=result,
                execution_time=execution_time,
            )
        except Exception as e:
            execution_time = asyncio.get_event_loop().time() - start_time
            return ToolResult(
                tool_name=tool_name,
                error=f"Tool '{tool_name}' execution failed: {str(e)}",
                execution_time=execution_time,
            )

    # ---- 工具描述生成 ----

    def get_tool_descriptions(self) -> list[dict[str, Any]]:
        """获取所有工具的描述（供 LLM 使用）

        Returns:
            OpenAI 格式的工具定义列表
        """
        descriptions = []
        for tool_name in self.list_tools():
            tool = self.get_tool(tool_name)
            if tool is None:
                continue
            if tool.config.enabled:
                descriptions.append(tool.get_dynamic_schema())
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
