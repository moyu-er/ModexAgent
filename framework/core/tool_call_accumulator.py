"""流式工具调用累积器

参考LangChain的实现,处理流式工具调用的累积和解析。
关键问题:
1. 工具调用参数可能分散在多个chunk中
2. 需要累积参数直到JSON完整
3. 需要支持多个工具调用
4. 需要处理不同格式的输入（对象、字典、列表）
"""

import json
from dataclasses import dataclass
from typing import Any

from ..core.types import ToolCall


@dataclass
class ToolCallChunk:
    """工具调用块 - 来自单个chunk的部分数据"""
    index: int                      # 工具调用索引(支持多个工具调用)
    id: str | None = None       # 工具调用ID
    name: str | None = None     # 函数名
    args: str | None = None     # 参数JSON字符串(可能不完整)


def _try_repair_json(raw: str) -> dict[str, Any]:
    """Attempt to repair common streaming JSON artifacts.

    Handles: trailing commas, unescaped control characters in string values.
    Returns empty dict if repair fails.
    """
    import re

    s = raw.strip()
    if not s:
        return {}

    # 1. Trailing comma before closing } or ]
    s = re.sub(r',\s*([}\]])', r'\1', s)

    # 2. Unescaped literal newlines / tabs inside string values
    s = s.replace('\r\n', '\\n').replace('\r', '\\n').replace('\n', '\\n').replace('\t', '\\t')

    # 3. Truncated — try closing open brackets
    open_curly = s.count('{') - s.count('}')
    open_square = s.count('[') - s.count(']')
    if open_curly > 0:
        s += '}' * open_curly
    if open_square > 0:
        s += ']' * open_square

    try:
        result = json.loads(s)
        return result if isinstance(result, dict) else {}
    except json.JSONDecodeError:
        return {}


@dataclass
class AccumulatingToolCall:
    """累积中的工具调用"""
    index: int
    id: str = ""
    name: str = ""
    args: str = ""  # 累积的参数字符串

    def add_chunk(self, chunk: ToolCallChunk) -> None:
        """添加一个chunk到累积器"""
        # ID: 只在有真实值时更新（流式响应中ID通常只在第一个chunk出现）
        if chunk.id:
            self.id = chunk.id
        # name: 使用 is not None 判断，因为需要累积不同的chunks
        if chunk.name is not None:
            self.name = chunk.name
        # args: 使用 is not None 判断，因为需要累积参数字符串
        if chunk.args is not None:
            self.args += chunk.args

    def is_complete(self) -> bool:
        """检查工具调用是否完整(可以解析参数)"""
        if not self.name:
            return False
        # 只有当args可以解析为有效的JSON时才认为完成
        # 空字符串表示还在等待参数
        if self.args == "":
            return False
        try:
            json.loads(self.args)
            return True
        except json.JSONDecodeError:
            return False

    def can_be_finalized(self) -> bool:
        """检查是否可以最终化(流结束时调用)"""
        # 有name就可以最终化,即使args不完整
        return bool(self.name)

    def is_empty(self) -> bool:
        """检查是否为空(既没有name也没有args)"""
        return not self.name and not self.args

    def to_tool_call(self, index: int = 0) -> ToolCall | None:
        """转换为ToolCall对象

        Args:
            index: 工具调用索引，用于生成默认ID

        Returns:
            ToolCall对象或None
        """
        if not self.name:
            return None

        # 解析参数
        args: dict[str, Any] = {}
        if self.args:
            try:
                args = json.loads(self.args)
            except json.JSONDecodeError:
                # 参数不完整,无法转换
                return None

        # 检查 call_id 是否存在
        if not self.id:
            raise ValueError(f"Tool call '{self.name}' is missing call_id. This indicates a parsing error.")

        return ToolCall(
            call_id=self.id,
            tool_name=self.name,
            arguments=args,
        )

    def to_partial_tool_call(self, index: int = 0) -> ToolCall:
        """转换为部分ToolCall(参数可能不完整)

        Args:
            index: 工具调用索引，用于生成默认ID

        Returns:
            ToolCall对象
        """
        args: dict[str, Any] = {}
        if self.args:
            try:
                args = json.loads(self.args)
            except json.JSONDecodeError:
                args = _try_repair_json(self.args)

        # 检查 call_id 是否存在
        if not self.id:
            raise ValueError(f"Tool call '{self.name}' is missing call_id. This indicates a parsing error.")

        return ToolCall(
            call_id=self.id,
            tool_name=self.name,
            arguments=args,
        )


class ToolCallAccumulator:
    """
    工具调用累积器。

    用于在流式输出中累积和解析工具调用。

    Example:
        accumulator = ToolCallAccumulator()

        # 模拟流式接收chunks
        for chunk in stream_chunks:
            if chunk.tool_calls:
                for tc in chunk.tool_calls:
                    chunk = ToolCallChunk(
                        index=tc.index,
                        id=tc.id,
                        name=tc.function.name,
                        args=tc.function.arguments,
                    )

                    # 添加chunk,检查是否有完成的工具调用
                    completed = accumulator.add_chunk(chunk)
                    if completed:
                        for tool_call in completed:
                            print(f"Tool: {tool_call.tool_name}")
                            print(f"Args: {tool_call.arguments}")

        # 获取所有累积中的(未完成的)
        pending = accumulator.get_pending()
    """

    def __init__(self):
        """初始化累积器"""
        self._accumulating: dict[int, AccumulatingToolCall] = {}
        self._completed: list[ToolCall] = []

    def add_chunk(self, chunk: ToolCallChunk) -> list[ToolCall]:
        """
        添加一个工具调用块。

        Args:
            chunk: 工具调用块

        Returns:
            本次添加后完成的ToolCall列表
        """
        index = chunk.index

        # 获取或创建累积中的工具调用
        if index not in self._accumulating:
            self._accumulating[index] = AccumulatingToolCall(index=index)

        acc = self._accumulating[index]
        acc.add_chunk(chunk)

        # 检查是否完成
        completed = []
        if acc.is_complete():
            tool_call = acc.to_tool_call(index=index)
            if tool_call:
                completed.append(tool_call)
                self._completed.append(tool_call)
                # 从累积中移除
                self._accumulating.pop(index)

        return completed

    def get_pending(self) -> list[AccumulatingToolCall]:
        """
        获取仍在累积中的工具调用。

        Returns:
            累积中的工具调用列表
        """
        return list(self._accumulating.values())

    def get_completed(self) -> list[ToolCall]:
        """
        获取已完成的工具调用。

        Returns:
            已完成的ToolCall列表
        """
        return self._completed.copy()

    def flush_pending(self) -> list[ToolCall]:
        """
        将所有累积中的工具调用转换为ToolCall(即使不完整)。

        在流结束时调用,获取所有未完成的工具调用。

        Returns:
            工具调用列表
        """
        result = []
        for acc in list(self._accumulating.values()):
            # 只处理可以最终化的工具调用
            if acc.can_be_finalized():
                tool_call = acc.to_tool_call(index=acc.index)
                if tool_call:
                    result.append(tool_call)
                else:
                    # 如果to_tool_call返回None,使用partial版本
                    result.append(acc.to_partial_tool_call(index=acc.index))
        self._accumulating.clear()
        return result

    def clear(self) -> None:
        """清空所有状态"""
        self._accumulating.clear()
        self._completed.clear()

    def __len__(self) -> int:
        """返回累积中的工具调用数量"""
        return len(self._accumulating)


def parse_tool_call_chunks_from_delta(
    tool_calls_data: Any,  # noqa: ANN401 — external data boundary: LiteLLM objects or dicts
) -> list[ToolCallChunk]:
    """
    从工具调用数据中解析工具调用块列表。

    支持多种格式:
    - LiteLLM 对象格式 (ChatCompletionDeltaToolCall)
    - 字典格式
    - 列表格式

    Args:
        tool_calls_data: 工具调用数据，可以是对象、字典或列表

    Returns:
        ToolCallChunk列表
    """
    chunks: list[ToolCallChunk] = []

    if not tool_calls_data:
        return chunks

    # 确保是列表
    if not isinstance(tool_calls_data, list):
        tool_calls_data = [tool_calls_data]

    for i, tc in enumerate(tool_calls_data):
        # 提取字段 - 支持对象和字典格式
        call_id = _get_value(tc, "id")

        name = None
        args = None

        # 提取 function 信息
        function = _get_value(tc, "function")
        if function:
            name = _get_value(function, "name")
            args = _get_value(function, "arguments")

        chunk = ToolCallChunk(
            index=_get_value(tc, "index", i),  # 优先使用 tc.index，否则使用枚举索引
            id=call_id,
            name=name,
            args=args,
        )
        chunks.append(chunk)

    return chunks


def _get_value(obj: Any, key: str, default: Any = None) -> Any:  # noqa: ANN401 — external data boundary
    """从对象或字典中获取值

    Args:
        obj: 对象或字典
        key: 键名
        default: 默认值

    Returns:
        值或默认值
    """
    if obj is None:
        return default

    # 字典格式
    if isinstance(obj, dict):
        return obj.get(key, default)

    # 对象格式
    if hasattr(obj, key):
        val = getattr(obj, key)
        # 排除未设置的 Pydantic 字段
        if val is not None:
            return val

    return default
