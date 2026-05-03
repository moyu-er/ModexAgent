"""工具执行器 - 支持Agent级别的工具调用"""

import asyncio
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from ..core.constants import ErrorMessages
from .toolkit import Toolkit
from .types import ToolResult


class ExecutionStrategy(str, Enum):
    """执行策略"""
    SEQUENTIAL = "sequential"  # 顺序执行
    PARALLEL = "parallel"      # 并行执行
    AUTO = "auto"              # 自动选择(默认)


@dataclass
class ExecutionContext:
    """执行上下文"""
    agent_id: str | None = None
    session_id: str | None = None
    user_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "session_id": self.session_id,
            "user_id": self.user_id,
            "metadata": self.metadata,
        }


@dataclass
class BatchToolResult:
    """批量工具执行结果"""
    results: list[ToolResult]
    total_execution_time_ms: float
    success_count: int
    failure_count: int

    def to_messages(self) -> list[dict[str, Any]]:
        """转换为消息列表(用于回填)"""
        return [r.to_message() for r in self.results]


class ToolExecutor:
    """
    工具执行器。
    
    支持:
    - 单个/批量工具调用
    - 顺序/并行执行
    - 执行上下文传递
    - 结果回填格式化
    
    Example:
        toolkit = Toolkit()
        # ... 添加工具 ...
        
        executor = ToolExecutor(toolkit)
        
        # 执行单个工具
        result = await executor.execute("get_weather", location="Beijing")
        
        # 批量执行
        calls = [
            {"name": "get_weather", "args": {"location": "Beijing"}},
            {"name": "calculate", "args": {"expression": "1+1"}},
        ]
        results = await executor.execute_batch(calls)
        
        # 回填到对话
        messages.extend(results.to_messages())
    """

    def __init__(
        self,
        toolkit: Toolkit,
        context: ExecutionContext | None = None,
        strategy: ExecutionStrategy = ExecutionStrategy.AUTO,
    ):
        """
        初始化执行器。
        
        Args:
            toolkit: 工具包
            context: 执行上下文
            strategy: 执行策略
        """
        self._toolkit = toolkit
        self._context = context or ExecutionContext()
        self._strategy = strategy

    async def execute(
        self,
        tool_name: str,
        call_id: str = "",
        **kwargs,
    ) -> ToolResult:
        """
        执行单个工具。
        
        Args:
            tool_name: 工具名称
            call_id: 调用ID(用于回填)
            **kwargs: 工具参数
        
        Returns:
            工具执行结果
        """
        start_time = time.time()

        tool = self._toolkit.get(tool_name)
        if not tool:
            return ToolResult(
                tool_name=tool_name,
                call_id=call_id,
                success=False,
                error=ErrorMessages.TOOL_NOT_FOUND.format(name=tool_name),
                execution_time_ms=0.0,
            )

        if not tool.metadata.enabled:
            return ToolResult(
                tool_name=tool_name,
                call_id=call_id,
                success=False,
                error=ErrorMessages.TOOL_DISABLED.format(name=tool_name),
                execution_time_ms=0.0,
            )

        try:
            # 执行工具
            result = await tool.execute(**kwargs)

            execution_time = (time.time() - start_time) * 1000

            return ToolResult(
                tool_name=tool_name,
                call_id=call_id,
                success=True,
                result=result,
                execution_time_ms=execution_time,
            )

        except Exception as e:
            execution_time = (time.time() - start_time) * 1000

            return ToolResult(
                tool_name=tool_name,
                call_id=call_id,
                success=False,
                error=str(e),
                execution_time_ms=execution_time,
            )

    async def execute_from_call(
        self,
        tool_call: dict[str, Any],
    ) -> ToolResult:
        """
        从工具调用对象执行。
        
        Args:
            tool_call: 工具调用对象 {id, name, arguments}
        
        Returns:
            工具执行结果
        """
        call_id = tool_call.get("id", "")
        name = tool_call.get("name", "")
        arguments = tool_call.get("arguments", {})

        if isinstance(arguments, str):
            import json
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError:
                return ToolResult(
                    tool_name=name,
                    call_id=call_id,
                    success=False,
                    error=ErrorMessages.INVALID_ARGUMENTS_JSON.format(args=arguments),
                    execution_time_ms=0.0,
                )

        return await self.execute(name, call_id, **arguments)

    async def execute_batch(
        self,
        tool_calls: list[dict[str, Any]],
        strategy: ExecutionStrategy | None = None,
    ) -> BatchToolResult:
        """
        批量执行工具调用。
        
        Args:
            tool_calls: 工具调用列表 [{name, args, id}]
            strategy: 执行策略,None则使用默认策略
        
        Returns:
            批量执行结果
        """
        start_time = time.time()
        strategy = strategy or self._strategy

        # 确定执行策略
        if strategy == ExecutionStrategy.AUTO:
            # 如果工具之间没有依赖,并行执行
            strategy = ExecutionStrategy.PARALLEL

        if strategy == ExecutionStrategy.PARALLEL:
            results = await self._execute_parallel(tool_calls)
        else:
            results = await self._execute_sequential(tool_calls)

        total_time = (time.time() - start_time) * 1000
        success_count = sum(1 for r in results if r.success)
        failure_count = len(results) - success_count

        return BatchToolResult(
            results=results,
            total_execution_time_ms=total_time,
            success_count=success_count,
            failure_count=failure_count,
        )

    async def _execute_sequential(
        self,
        tool_calls: list[dict[str, Any]],
    ) -> list[ToolResult]:
        """顺序执行"""
        results = []
        for call in tool_calls:
            result = await self.execute_from_call(call)
            results.append(result)
        return results

    async def _execute_parallel(
        self,
        tool_calls: list[dict[str, Any]],
    ) -> list[ToolResult]:
        """并行执行"""
        tasks = [
            self.execute_from_call(call)
            for call in tool_calls
        ]
        return await asyncio.gather(*tasks)

    def with_context(self, **kwargs) -> "ToolExecutor":
        """
        创建带新上下文的执行器。
        
        Example:
            new_executor = executor.with_context(
                agent_id="agent_1",
                session_id="session_123",
            )
        """
        new_context = ExecutionContext(
            agent_id=kwargs.get("agent_id", self._context.agent_id),
            session_id=kwargs.get("session_id", self._context.session_id),
            user_id=kwargs.get("user_id", self._context.user_id),
            metadata={**self._context.metadata, **kwargs.get("metadata", {})},
        )
        return ToolExecutor(self._toolkit, new_context, self._strategy)


class ToolCallParser:
    """
    工具调用解析器。
    
    支持从各种格式解析工具调用:
    - OpenAI格式
    - LiteLLM格式
    - 流式chunk格式
    """

    @staticmethod
    def parse_from_openai(message: dict[str, Any]) -> list[dict[str, Any]]:
        """
        从OpenAI消息解析工具调用。
        
        Args:
            message: OpenAI消息对象
        
        Returns:
            工具调用列表 [{id, name, arguments}]
        """
        tool_calls = message.get("tool_calls", [])
        if not tool_calls:
            return []

        result = []
        for tc in tool_calls:
            function = tc.get("function", {})
            result.append({
                "id": tc.get("id", ""),
                "name": function.get("name", ""),
                "arguments": function.get("arguments", {}),
            })

        return result

    @staticmethod
    def parse_from_litellm(message: Any) -> list[dict[str, Any]]:
        """
        从LiteLLM消息解析工具调用。
        
        Args:
            message: LiteLLM消息对象
        
        Returns:
            工具调用列表
        """
        # LiteLLM格式与OpenAI类似
        if hasattr(message, 'tool_calls') and message.tool_calls:
            result = []
            for tc in message.tool_calls:
                function = tc.function if hasattr(tc, 'function') else tc.get("function", {})
                result.append({
                    "id": getattr(tc, 'id', tc.get("id", "")),
                    "name": getattr(function, 'name', function.get("name", "")),
                    "arguments": getattr(function, 'arguments', function.get("arguments", {})),
                })
            return result

        # 尝试作为dict解析
        if isinstance(message, dict):
            return ToolCallParser.parse_from_openai(message)

        return []

    @staticmethod
    def parse_from_stream_chunk(chunk: Any) -> dict[str, Any] | None:
        """
        从流式chunk解析工具调用delta。
        
        Args:
            chunk: 流式chunk
        
        Returns:
            工具调用delta或None
        """
        # OpenAI/LiteLLM流式格式
        delta = getattr(chunk, 'delta', None)
        if not delta and isinstance(chunk, dict):
            delta = chunk.get('delta', {})

        if not delta:
            return None

        tool_calls = getattr(delta, 'tool_calls', None)
        if tool_calls is None and isinstance(delta, dict):
            tool_calls = delta.get('tool_calls')

        if not tool_calls:
            return None

        # 返回第一个tool_call delta
        if isinstance(tool_calls, list) and len(tool_calls) > 0:
            tc = tool_calls[0]
            return {
                "index": getattr(tc, 'index', 0),
                "id": getattr(tc, 'id', None),
                "name": getattr(getattr(tc, 'function', None), 'name', None),
                "arguments": getattr(getattr(tc, 'function', None), 'arguments', None),
            }

        return None


# 便捷函数
async def execute_tools(
    toolkit: Toolkit,
    tool_calls: list[dict[str, Any]],
    context: ExecutionContext | None = None,
) -> BatchToolResult:
    """
    便捷函数:执行工具调用。
    
    Example:
        results = await execute_tools(toolkit, [
            {"name": "get_weather", "arguments": {"location": "Beijing"}, "id": "call_1"},
        ])
        
        messages.extend(results.to_messages())
    """
    executor = ToolExecutor(toolkit, context)
    return await executor.execute_batch(tool_calls)
