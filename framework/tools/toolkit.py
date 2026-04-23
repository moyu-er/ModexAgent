"""工具包 - Agent级别的工具管理"""

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set, Union
from copy import deepcopy

from .types import ToolDefinition, ToolFunction, ToolResult, infer_parameters_from_function
from .registry import ToolRegistry
from ..core.constants import ErrorMessages, DefaultValues


@dataclass
class ToolMetadata:
    """工具元数据"""
    name: str
    description: str
    tags: Set[str] = field(default_factory=set)
    category: str = DefaultValues.TOOL_CATEGORY
    version: str = DefaultValues.TOOL_VERSION
    author: Optional[str] = None
    enabled: bool = True
    requires_confirmation: bool = False
    rate_limit: Optional[int] = None
    timeout_seconds: float = DefaultValues.TOOL_TIMEOUT_SECONDS
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "tags": list(self.tags),
            "category": self.category,
            "version": self.version,
            "author": self.author,
            "enabled": self.enabled,
            "requires_confirmation": self.requires_confirmation,
            "rate_limit": self.rate_limit,
            "timeout_seconds": self.timeout_seconds,
        }


class ToolWrapper:
    """
    工具包装器。
    
    包装工具函数,添加元数据和AOP支持。
    """
    
    def __init__(
        self,
        func: ToolFunction,
        definition: ToolDefinition,
        metadata: Optional[ToolMetadata] = None,
    ):
        self._func = func
        self._definition = definition
        self._metadata = metadata or ToolMetadata(
            name=definition.name,
            description=definition.description,
        )
        self._before_hooks: List[Callable] = []
        self._after_hooks: List[Callable] = []
        self._around_hooks: List[Callable] = []
    
    @property
    def name(self) -> str:
        return self._definition.name
    
    @property
    def definition(self) -> ToolDefinition:
        return self._definition
    
    @property
    def metadata(self) -> ToolMetadata:
        return self._metadata
    
    def add_before_hook(self, hook: Callable) -> "ToolWrapper":
        """添加前置钩子"""
        self._before_hooks.append(hook)
        return self
    
    def add_after_hook(self, hook: Callable) -> "ToolWrapper":
        """添加后置钩子"""
        self._after_hooks.append(hook)
        return self
    
    def add_around_hook(self, hook: Callable) -> "ToolWrapper":
        """添加环绕钩子"""
        self._around_hooks.append(hook)
        return self
    
    def _convert_args_to_pydantic(self, **kwargs) -> Dict[str, Any]:
        """
        将参数转换为 Pydantic 模型（如果需要）。
        
        当函数期望 Pydantic BaseModel 参数但传入的是 dict 时，
        自动进行转换。支持嵌套模型。
        """
        from typing import get_type_hints, get_args, Annotated, get_origin
        
        def _is_pydantic_model(t: type) -> bool:
            """检查类型是否是 Pydantic BaseModel"""
            if not isinstance(t, type):
                return False
            return hasattr(t, '__mro__') and any(
                base.__name__ == 'BaseModel' for base in t.__mro__
            )
        
        def _convert_value(value: Any, target_type: type) -> Any:
            """递归转换值为目标类型"""
            if value is None:
                return None
            
            # 解包 Annotated 类型
            origin = get_origin(target_type)
            if origin is Annotated:
                target_type = get_args(target_type)[0]
            
            # 处理列表
            origin_type = get_origin(target_type)
            if origin_type is list or target_type is list:
                args = get_args(target_type)
                if args and isinstance(value, list):
                    item_type = args[0]
                    return [_convert_value(item, item_type) for item in value]
                return value
            
            # 处理 Optional
            if origin_type is type(None) or str(target_type).startswith('typing.Optional'):
                args = get_args(target_type)
                if args:
                    inner_type = args[0]
                    return _convert_value(value, inner_type)
                return value
            
            # 处理 Pydantic 模型
            if _is_pydantic_model(target_type) and isinstance(value, dict):
                # 获取模型字段的类型提示
                try:
                    model_hints = get_type_hints(target_type, include_extras=True)
                    converted_dict = {}
                    for k, v in value.items():
                        if k in model_hints:
                            converted_dict[k] = _convert_value(v, model_hints[k])
                        else:
                            converted_dict[k] = v
                    # 使用 model_construct 跳过验证（处理部分参数）
                    if hasattr(target_type, 'model_construct'):
                        return target_type.model_construct(**converted_dict)
                    return target_type(**converted_dict)
                except Exception:
                    # 转换失败，返回原始值
                    pass
            
            return value
        
        try:
            type_hints = get_type_hints(self._func, include_extras=True)
        except Exception:
            return kwargs
        
        converted = dict(kwargs)
        
        for param_name, param_type in type_hints.items():
            if param_name not in converted:
                continue
            
            value = converted[param_name]
            
            # 如果值已经是目标类型，跳过
            try:
                origin = get_origin(param_type)
                actual_type = get_args(param_type)[0] if origin is Annotated else param_type
                if isinstance(value, actual_type):
                    continue
            except (TypeError, IndexError):
                pass
            
            # 递归转换
            converted[param_name] = _convert_value(value, param_type)
        
        return converted
    
    async def execute(self, **kwargs) -> Any:
        """执行工具,支持AOP"""
        import asyncio
        import time
        
        # 检查是否启用
        if not self._metadata.enabled:
            raise ToolDisabledError(
                ErrorMessages.TOOL_DISABLED.format(name=self.name)
            )
        
        # 转换参数（支持 Pydantic 模型）
        kwargs = self._convert_args_to_pydantic(**kwargs)
        
        # 执行前置钩子
        context = {"tool_name": self.name, "args": kwargs}
        for hook in self._before_hooks:
            if asyncio.iscoroutinefunction(hook):
                await hook(context)
            else:
                hook(context)
        
        start_time = time.time()
        
        # 执行环绕钩子或实际函数
        if self._around_hooks:
            # 使用环绕钩子
            result = await self._execute_with_around_hooks(context, **kwargs)
        else:
            # 直接执行
            if asyncio.iscoroutinefunction(self._func):
                result = await self._func(**kwargs)
            else:
                result = self._func(**kwargs)
        
        execution_time = (time.time() - start_time) * 1000
        
        # 执行后置钩子
        context["result"] = result
        context["execution_time_ms"] = execution_time
        for hook in self._after_hooks:
            if asyncio.iscoroutinefunction(hook):
                await hook(context)
            else:
                hook(context)
        
        return result
    
    async def _execute_with_around_hooks(self, context: Dict, **kwargs) -> Any:
        """使用环绕钩子执行"""
        import asyncio
        
        # 构建调用链
        async def call_next(idx: int, **args) -> Any:
            if idx < len(self._around_hooks):
                hook = self._around_hooks[idx]
                if asyncio.iscoroutinefunction(hook):
                    return await hook(context, lambda **a: call_next(idx + 1, **a), **args)
                else:
                    return hook(context, lambda **a: call_next(idx + 1, **a), **args)
            else:
                # 执行实际函数
                if asyncio.iscoroutinefunction(self._func):
                    return await self._func(**args)
                else:
                    return self._func(**args)
        
        return await call_next(0, **kwargs)
    
    def to_openai_format(self) -> Dict[str, Any]:
        """转换为OpenAI格式"""
        return self._definition.to_openai_format()


class ToolDisabledError(Exception):
    """工具被禁用错误"""
    pass


class Toolkit:
    """
    工具包 - Agent级别的工具集合。
    
    每个Agent可以有自己的Toolkit,支持:
    - 从全局注册表选择工具
    - 自定义工具列表
    - 工具元数据管理
    - AOP拦截器
    
    Example:
        # 创建工具包
        toolkit = Toolkit()
        
        # 从全局注册表添加工具
        toolkit.add_from_registry("get_weather", "calculate")
        
        # 添加自定义工具
        @toolkit.add
        def my_custom_tool(data: str) -> str:
            return data.upper()
        
        # 配置工具元数据
        toolkit.configure_tool("get_weather", requires_confirmation=True)
        
        # 添加AOP拦截器
        toolkit.add_before_hook("get_weather", log_tool_call)
        
        # 获取工具定义
        tools = toolkit.to_openai_format()
    """
    
    def __init__(self, registry: Optional[ToolRegistry] = None):
        """
        初始化工具包。
        
        Args:
            registry: 全局注册表,用于从中选择工具
        """
        self._registry = registry
        self._tools: Dict[str, ToolWrapper] = {}
        self._tool_order: List[str] = []  # 保持工具顺序
    
    def add_from_registry(
        self,
        *tool_names: str,
        metadata: Optional[Dict[str, ToolMetadata]] = None,
    ) -> "Toolkit":
        """
        从全局注册表添加工具。
        
        Args:
            tool_names: 工具名称列表
            metadata: 工具元数据配置 {tool_name: metadata}
        
        Returns:
            self,支持链式调用
        """
        if not self._registry:
            raise ValueError(ErrorMessages.NO_REGISTRY_PROVIDED)

        for name in tool_names:
            func = self._registry.get(name)
            definition = self._registry.get_definition(name)

            if not func or not definition:
                raise ValueError(
                    ErrorMessages.TOOL_NOT_IN_REGISTRY.format(name=name)
                )
            
            meta = metadata.get(name) if metadata else None
            self._add_tool(name, func, definition, meta)
        
        return self
    
    def add(
        self,
        func: Optional[ToolFunction] = None,
        *,
        name: Optional[str] = None,
        description: Optional[str] = None,
        metadata: Optional[ToolMetadata] = None,
    ) -> Union[ToolFunction, Callable[[ToolFunction], ToolFunction]]:
        """
        添加自定义工具。
        
        可以作为装饰器使用。
        
        Example:
            @toolkit.add
            def my_tool(x: int) -> int:
                return x * 2
            
            @toolkit.add(name="custom", description="Custom tool")
            def another_tool():
                pass
        """
        def decorator(f: ToolFunction) -> ToolFunction:
            tool_name = name or f.__name__
            tool_desc = description or (f.__doc__ or "").strip()
            
            definition = ToolDefinition(
                name=tool_name,
                description=tool_desc,
                parameters=infer_parameters_from_function(f),
            )
            
            self._add_tool(tool_name, f, definition, metadata)
            return f
        
        if func is None:
            return decorator
        else:
            return decorator(func)
    
    def add_functions(
        self,
        functions: List[ToolFunction],
        *,
        names: Optional[List[str]] = None,
        descriptions: Optional[List[str]] = None,
        metadatas: Optional[List[Optional[ToolMetadata]]] = None,
        auto_parse_metadata: bool = True,
    ) -> "Toolkit":
        """
        批量添加函数列表作为工具。
        
        支持为不同Agent配置不同的工具列表。
        
        Args:
            functions: 函数列表
            names: 工具名称列表(可选,默认使用函数名)
            descriptions: 工具描述列表(可选,默认使用docstring)
            metadatas: 工具元数据列表(可选)
            auto_parse_metadata: 是否自动从函数解析元数据(标签、类别等)
        
        Returns:
            self,支持链式调用
        
        Example:
            def get_weather(location: str) -> str:
                \"\"\"
                获取指定位置的天气。
                
                Args:
                    location: 城市名称
                
                Tags: weather, external
                Category: utility
                \"\"\"
                return f"Weather in {location}: Sunny"
            
            def calculate(expr: str) -> str:
                return str(eval(expr))
            
            # Agent 1: 只有天气工具
            agent1_tools = Toolkit()
            agent1_tools.add_functions([get_weather])
            
            # Agent 2: 有天气和计算工具
            agent2_tools = Toolkit()
            agent2_tools.add_functions([get_weather, calculate])
            
            # 带自定义元数据
            agent3_tools = Toolkit()
            agent3_tools.add_functions(
                [get_weather, calculate],
                names=["weather", "calc"],
                descriptions=["获取天气", "计算表达式"],
                metadatas=[
                    ToolMetadata(name="weather", requires_confirmation=False),
                    ToolMetadata(name="calc", requires_confirmation=True),
                ]
            )
        """
        from .metadata_parser import parse_function_metadata
        
        for i, func in enumerate(functions):
            tool_name = names[i] if names and i < len(names) else func.__name__
            
            # 自动解析元数据
            parsed_meta = parse_function_metadata(func) if auto_parse_metadata else None
            
            # 使用传入的描述或解析的描述
            if descriptions and i < len(descriptions):
                tool_desc = descriptions[i]
            elif parsed_meta and parsed_meta.description:
                tool_desc = parsed_meta.description
            else:
                tool_desc = (func.__doc__ or "").strip()
            
            # 构建或合并元数据
            if metadatas and i < len(metadatas) and metadatas[i]:
                meta = metadatas[i]
                # 如果自动解析了元数据,合并标签和类别
                if parsed_meta:
                    if not meta.tags and parsed_meta.tags:
                        meta.tags = parsed_meta.tags
                    if meta.category == DefaultValues.TOOL_CATEGORY and parsed_meta.category != DefaultValues.TOOL_CATEGORY:
                        meta.category = parsed_meta.category
            elif parsed_meta:
                # 从解析的元数据创建
                meta = ToolMetadata(
                    name=tool_name,
                    description=tool_desc,
                    tags=parsed_meta.tags,
                    category=parsed_meta.category,
                )
            else:
                meta = None
            
            definition = ToolDefinition(
                name=tool_name,
                description=tool_desc,
                parameters=infer_parameters_from_function(func),
            )
            
            self._add_tool(tool_name, func, definition, meta)
        
        return self
    
    def add_function(
        self,
        func: ToolFunction,
        *,
        name: Optional[str] = None,
        description: Optional[str] = None,
        metadata: Optional[ToolMetadata] = None,
    ) -> "Toolkit":
        """
        添加单个函数作为工具(链式调用风格)。
        
        Args:
            func: 函数
            name: 工具名称(可选)
            description: 工具描述(可选)
            metadata: 工具元数据(可选)
        
        Returns:
            self,支持链式调用
        
        Example:
            def get_weather(location: str) -> str:
                return f"Weather in {location}: Sunny"
            
            toolkit = Toolkit()
            toolkit.add_function(get_weather, name="weather", metadata=ToolMetadata(requires_confirmation=False))
                  .add_function(calculate, name="calc")
        """
        return self.add_functions(
            [func],
            names=[name] if name else None,
            descriptions=[description] if description else None,
            metadatas=[metadata],
        )
    
    def _add_tool(
        self,
        name: str,
        func: ToolFunction,
        definition: ToolDefinition,
        metadata: Optional[ToolMetadata] = None,
    ) -> None:
        """添加工具到工具包"""
        wrapper = ToolWrapper(func, definition, metadata)
        self._tools[name] = wrapper
        if name not in self._tool_order:
            self._tool_order.append(name)
    
    def remove(self, name: str) -> bool:
        """移除工具"""
        if name in self._tools:
            del self._tools[name]
            self._tool_order.remove(name)
            return True
        return False
    
    def get(self, name: str) -> Optional[ToolWrapper]:
        """获取工具包装器"""
        return self._tools.get(name)
    
    def has_tool(self, name: str) -> bool:
        """检查工具是否存在"""
        return name in self._tools
    
    def list_tools(self) -> List[str]:
        """列出所有工具名称"""
        return self._tool_order.copy()
    
    def configure_tool(
        self,
        name: str,
        **metadata_kwargs,
    ) -> "Toolkit":
        """
        配置工具元数据。
        
        Example:
            toolkit.configure_tool(
                "get_weather",
                requires_confirmation=True,
                timeout_seconds=10.0,
                tags={"weather", "external"},
            )
        """
        tool = self._tools.get(name)
        if not tool:
            raise ValueError(
                ErrorMessages.TOOL_NOT_FOUND_IN_TOOLKIT.format(name=name)
            )
        
        for key, value in metadata_kwargs.items():
            if hasattr(tool.metadata, key):
                setattr(tool.metadata, key, value)
        
        return self
    
    def enable_tool(self, name: str) -> "Toolkit":
        """启用工具"""
        return self.configure_tool(name, enabled=True)
    
    def disable_tool(self, name: str) -> "Toolkit":
        """禁用工具"""
        return self.configure_tool(name, enabled=False)
    
    # AOP 方法
    def add_before_hook(
        self,
        tool_name: str,
        hook: Callable,
    ) -> "Toolkit":
        """为指定工具添加前置钩子"""
        tool = self._tools.get(tool_name)
        if tool:
            tool.add_before_hook(hook)
        return self
    
    def add_after_hook(
        self,
        tool_name: str,
        hook: Callable,
    ) -> "Toolkit":
        """为指定工具添加后置钩子"""
        tool = self._tools.get(tool_name)
        if tool:
            tool.add_after_hook(hook)
        return self
    
    def add_around_hook(
        self,
        tool_name: str,
        hook: Callable,
    ) -> "Toolkit":
        """为指定工具添加环绕钩子"""
        tool = self._tools.get(tool_name)
        if tool:
            tool.add_around_hook(hook)
        return self
    
    def add_global_before_hook(self, hook: Callable) -> "Toolkit":
        """为所有工具添加前置钩子"""
        for tool in self._tools.values():
            tool.add_before_hook(hook)
        return self
    
    def add_global_after_hook(self, hook: Callable) -> "Toolkit":
        """为所有工具添加后置钩子"""
        for tool in self._tools.values():
            tool.add_after_hook(hook)
        return self
    
    # 格式转换
    def to_openai_format(self, tool_names: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        """转换为OpenAI格式"""
        names = tool_names or self._tool_order
        return [
            self._tools[name].to_openai_format()
            for name in names
            if name in self._tools and self._tools[name].metadata.enabled
        ]
    
    def to_litellm_format(self, tool_names: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        """转换为LiteLLM格式"""
        return self.to_openai_format(tool_names)
    
    def clone(self) -> "Toolkit":
        """克隆工具包"""
        new_toolkit = Toolkit(self._registry)
        new_toolkit._tools = deepcopy(self._tools)
        new_toolkit._tool_order = self._tool_order.copy()
        return new_toolkit
    
    def clear(self) -> None:
        """清空所有工具"""
        self._tools.clear()
        self._tool_order.clear()
    
    def __len__(self) -> int:
        return len(self._tools)
    
    def __contains__(self, name: str) -> bool:
        return name in self._tools
    
    # 兼容 ToolRegistry 接口 (用于 loop/react.py)
    
    def get_definitions(self) -> List[Dict[str, Any]]:
        """
        获取所有工具的 schema 定义（兼容 ToolRegistry 接口）。
        
        Returns:
            工具定义列表
        """
        return self.to_openai_format()
    
    async def execute(self, tool_name: str, arguments: Dict[str, Any]) -> str:
        """
        执行工具（兼容 ToolRegistry 接口）。
        
        Args:
            tool_name: 工具名称
            arguments: 工具参数
        
        Returns:
            工具执行结果字符串
        """
        tool = self._tools.get(tool_name)
        if not tool:
            raise ValueError(f"Tool '{tool_name}' not found")
        
        import asyncio
        result = await tool.execute(**arguments)
        return str(result)
    
    def has(self, tool_name: str) -> bool:
        """
        检查工具是否存在（兼容 ToolRegistry 接口）。
        
        Args:
            tool_name: 工具名称
        
        Returns:
            是否存在
        """
        return self.has_tool(tool_name)
    
    def register(self, tool_wrapper: ToolWrapper) -> None:
        """
        注册工具包装器（兼容 ToolRegistry 接口）。
        
        Args:
            tool_wrapper: 工具包装器实例
        """
        if tool_wrapper.name in self._tools:
            raise ValueError(f"Tool '{tool_wrapper.name}' already registered")
        self._tools[tool_wrapper.name] = tool_wrapper
        if tool_wrapper.name not in self._tool_order:
            self._tool_order.append(tool_wrapper.name)
    
    def unregister(self, tool_name: str) -> bool:
        """
        注销工具（兼容 ToolRegistry 接口）。
        
        Args:
            tool_name: 工具名称
        
        Returns:
            是否成功注销
        """
        return self.remove(tool_name)
