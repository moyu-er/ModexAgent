"""工具管理器 - 抽象基类和实现

提供 ToolManager 抽象层，支持工具注册、配置和并行执行调度。
"""

import asyncio
import logging
from abc import ABC, abstractmethod
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from enum import Enum, auto
from typing import Any

from framework.core.tool import DynamicSchemaProvider

logger = logging.getLogger(__name__)


class ToolExecutionMode(Enum):
    """工具执行模式"""
    SEQUENTIAL = auto()      # 顺序执行（同步阻塞）
    PARALLEL = auto()        # 并行执行（在线程池中）
    ASYNC = auto()           # 异步执行（协程）


@dataclass
class ToolConfig:
    """单个工具的配置"""
    timeout: float = 30.0                    # 执行超时（秒）
    execution_mode: ToolExecutionMode = ToolExecutionMode.ASYNC
    retry_count: int = 0                     # 失败重试次数
    retry_delay: float = 1.0                 # 重试间隔（秒）
    enabled: bool = True                     # 是否启用


@dataclass
class ToolManagerConfig:
    """ToolManager 全局配置"""
    max_workers: int = 10                    # 线程池最大工作线程数
    default_timeout: float = 30.0            # 默认超时
    default_execution_mode: ToolExecutionMode = ToolExecutionMode.ASYNC
    enable_parallel: bool = True             # 是否允许并行执行多个工具
    parallel_max_workers: int = 5            # 并行执行时的最大并发数


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
    ):
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
        raise NotImplementedError("Tool must define 'description' either via __init__ or as a property")

    @property
    def parameters(self) -> dict[str, Any]:
        """工具参数定义"""
        if self._parameters is not None:
            return self._parameters
        raise NotImplementedError("Tool must define 'parameters' either via __init__ or as a property")

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
    ):
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
            from framework.memory.core.message import ContentFormat
            msg["content_format"] = ContentFormat.XML.value
            msg["truncatable_paths"] = paths
        return msg


class ToolManager(ABC):
    """工具管理器抽象基类

    职责：
    1. 工具注册/注销（动态扩展）
    2. 工具执行调度（顺序/并行/异步）
    3. 工具配置管理
    4. 生成工具描述给 LLM

    不处理：
    - 具体的工具实现（由 Tool 子类实现）
    - LLM 调用
    """

    def __init__(self, config: ToolManagerConfig | None = None):
        self.config = config or ToolManagerConfig()
        self._thread_pool: ThreadPoolExecutor | None = None

    async def __aenter__(self):
        """异步上下文管理器入口"""
        await self.startup()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """异步上下文管理器退出"""
        await self.shutdown()

    async def startup(self) -> None:
        """启动 ToolManager，初始化线程池等资源"""
        if self.config.max_workers > 0:
            self._thread_pool = ThreadPoolExecutor(
                max_workers=self.config.max_workers,
                thread_name_prefix="tool_executor_"
            )
            logger.debug(f"ToolManager started with {self.config.max_workers} workers")

    async def shutdown(self) -> None:
        """关闭 ToolManager，释放资源"""
        if self._thread_pool:
            self._thread_pool.shutdown(wait=True)
            self._thread_pool = None
            logger.debug("ToolManager shutdown complete")

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

    def has_tool(self, tool_name: str) -> bool:
        """检查工具是否已注册"""
        return self.is_registered(tool_name)

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

        # 防御性检查：确保工具实例有 config 属性
        if not hasattr(tool, 'config') or tool.config is None:
            error_msg = f"Tool '{tool_name}' has no config attribute (tool type: {type(tool).__name__})"
            logger.error(error_msg)
            return ToolResult(
                tool_name=tool_name,
                error=error_msg,
            )

        if not tool.config.enabled:
            logger.warning(f"Tool disabled: {tool_name}")
            return ToolResult(
                tool_name=tool_name,
                error=f"Tool '{tool_name}' is disabled",
            )

        config = tool.config
        start_time = asyncio.get_event_loop().time()

        # 执行重试逻辑
        attempt = 0
        last_error = None

        while attempt <= config.retry_count:
            try:
                # 根据执行模式选择执行方式
                if config.execution_mode == ToolExecutionMode.ASYNC:
                    result = await self._execute_async(tool, arguments, config)
                elif config.execution_mode == ToolExecutionMode.PARALLEL:
                    result = await self._execute_parallel(tool, arguments, config)
                else:  # SEQUENTIAL
                    result = await self._execute_sequential(tool, arguments, config)

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

            except TimeoutError:
                execution_time = asyncio.get_event_loop().time() - start_time
                last_error = f"Tool '{tool_name}' execution timed out after {config.timeout}s"
                logger.warning(f"Tool {tool_name} timeout (attempt {attempt + 1}/{config.retry_count + 1})")

            except Exception as e:
                execution_time = asyncio.get_event_loop().time() - start_time
                last_error = f"Tool '{tool_name}' execution failed: {str(e)}"
                logger.warning(f"Tool {tool_name} error (attempt {attempt + 1}/{config.retry_count + 1}): {e}")

            attempt += 1
            if attempt <= config.retry_count:
                await asyncio.sleep(config.retry_delay)

        # 所有重试都失败了
        return ToolResult(
            tool_name=tool_name,
            error=last_error,
            execution_time=asyncio.get_event_loop().time() - start_time,
        )

    async def execute_batch(
        self,
        tool_calls: list[dict[str, Any]],  # [{"tool_name": str, "arguments": dict}, ...]
        parallel: bool | None = None,
    ) -> list[ToolResult]:
        """批量执行多个工具

        Args:
            tool_calls: 工具调用列表
            parallel: 是否并行执行（默认使用配置）

        Returns:
            List[ToolResult]: 执行结果列表（与输入顺序一致）
        """
        if not tool_calls:
            return []

        use_parallel = parallel if parallel is not None else self.config.enable_parallel

        if use_parallel and len(tool_calls) > 1:
            # 并行执行
            semaphore = asyncio.Semaphore(self.config.parallel_max_workers)

            async def execute_with_limit(tc: dict[str, Any]) -> ToolResult:
                async with semaphore:
                    return await self.execute(
                        tc["tool_name"],
                        tc.get("arguments", {}),
                    )

            tasks = [execute_with_limit(tc) for tc in tool_calls]
            return await asyncio.gather(*tasks)
        else:
            # 顺序执行
            results = []
            for tc in tool_calls:
                result = await self.execute(
                    tc["tool_name"],
                    tc.get("arguments", {}),
                )
                results.append(result)
            return results

    async def _execute_async(
        self,
        tool: Tool,
        arguments: dict[str, Any],
        config: ToolConfig,
    ) -> Any:
        """异步执行（协程）"""
        return await asyncio.wait_for(
            tool.execute(**arguments),
            timeout=config.timeout,
        )

    async def _execute_parallel(
        self,
        tool: Tool,
        arguments: dict[str, Any],
        config: ToolConfig,
    ) -> Any:
        """在线程池中并行执行"""
        if self._thread_pool is None:
            raise RuntimeError("Thread pool not initialized")

        loop = asyncio.get_event_loop()
        return await asyncio.wait_for(
            loop.run_in_executor(
                self._thread_pool,
                lambda: asyncio.run(tool.execute(**arguments)),
            ),
            timeout=config.timeout,
        )

    async def _execute_sequential(
        self,
        tool: Tool,
        arguments: dict[str, Any],
        config: ToolConfig,
    ) -> Any:
        """顺序执行（同步阻塞）"""
        return await self._execute_async(tool, arguments, config)

    # ---- 工具描述生成 ----

    def get_tool_descriptions(self) -> list[dict[str, Any]]:
        """获取所有工具的描述（供 LLM 使用）

        Returns:
            OpenAI 格式的工具定义列表
        """
        descriptions = []
        for tool_name in self.list_tools():
            tool = self.get_tool(tool_name)
            if tool is None or not hasattr(tool, 'config'):
                continue
            if tool.config.enabled:
                descriptions.append(tool.get_dynamic_schema())
        return descriptions



class InMemoryToolManager(ToolManager):
    """内存中的工具管理器实现"""

    def __init__(self, config: ToolManagerConfig | None = None):
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

    def has_tool(self, tool_name: str) -> bool:
        """检查工具是否已注册"""
        return self.is_registered(tool_name)

    @property
    def tools(self) -> dict[str, Tool]:
        """所有已注册的工具（按名称索引）。调试用，修改 dict 不影响管理器。"""
        return dict(self._tools)

    def __contains__(self, tool_name: str) -> bool:
        """支持 'tool_name in tool_manager' 语法"""
        return self.is_registered(tool_name)


class FunctionalTool(Tool):
    """基于函数的工具实现

    允许将普通函数包装为 Tool 实例。
    """

    def __init__(
        self,
        name: str,
        description: str,
        parameters: dict[str, Any],
        func: Callable[..., Any],
        config: ToolConfig | None = None,
    ):
        super().__init__(name, description, parameters, config)
        self._func = func

    async def execute(self, **kwargs) -> Any:
        """执行函数"""
        result = self._func(**kwargs)
        # 如果结果是协程，等待它
        if asyncio.iscoroutine(result):
            return await result
        return result
