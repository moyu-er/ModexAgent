"""工具类型定义

支持复杂类型参数，借鉴 LangChain 的设计：
- 基本类型: str, int, float, bool
- 容器类型: List[T], Dict[K, V], Optional[T]
- 复杂类型: Pydantic BaseModel, TypedDict, dataclass
- 枚举类型: Enum, Literal
"""

import inspect
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional, Union, get_type_hints

from ..core.constants import (
    ToolSchemaConstants,
)


class ToolParameterType(str, Enum):
    """参数类型"""

    STRING = "string"
    INTEGER = "integer"
    NUMBER = "number"
    BOOLEAN = "boolean"
    ARRAY = "array"
    OBJECT = "object"


@dataclass
class ToolParameter:
    """工具参数定义

    支持复杂嵌套结构，通过 items 和 properties 定义。

    Example:
        # 简单参数
        param = ToolParameter(
            name="query",
            type=ToolParameterType.STRING,
            description="搜索查询"
        )

        # 数组参数
        param = ToolParameter(
            name="tags",
            type=ToolParameterType.ARRAY,
            description="标签列表",
            items=ToolParameter(
                name="tag",
                type=ToolParameterType.STRING,
                description="单个标签"
            )
        )

        # 对象参数
        param = ToolParameter(
            name="address",
            type=ToolParameterType.OBJECT,
            description="地址信息",
            properties={
                "street": ToolParameter(...),
                "city": ToolParameter(...),
            }
        )
    """

    name: str
    type: ToolParameterType
    description: str
    required: bool = True
    enum: list[str] | None = None
    default: Any = None

    # 复杂类型支持
    items: Optional["ToolParameter"] = None  # 用于 ARRAY 类型
    properties: dict[str, "ToolParameter"] | None = None  # 用于 OBJECT 类型
    additional_properties: bool | None = None  # 是否允许额外属性

    def to_json_schema(self) -> dict[str, Any]:
        """转换为JSON Schema格式"""
        schema: dict[str, Any] = {
            "type": self.type.value,
            "description": self.description,
        }

        if self.enum:
            schema["enum"] = self.enum

        if self.default is not None:
            schema["default"] = self.default

        # 数组类型的元素定义
        if self.type == ToolParameterType.ARRAY and self.items:
            schema["items"] = self.items.to_json_schema()

        # 对象类型的属性定义
        if self.type == ToolParameterType.OBJECT and self.properties:
            schema["properties"] = {
                name: prop.to_json_schema() for name, prop in self.properties.items()
            }
            required_props = [name for name, prop in self.properties.items() if prop.required]
            if required_props:
                schema["required"] = required_props

        if self.additional_properties is not None:
            schema["additionalProperties"] = self.additional_properties

        return schema


@dataclass
class ToolDefinition:
    """工具定义"""

    name: str
    description: str
    parameters: list[ToolParameter]

    def to_openai_format(self) -> dict[str, Any]:
        """转换为OpenAI工具格式"""
        properties = {}
        required = []

        for param in self.parameters:
            properties[param.name] = param.to_json_schema()
            if param.required:
                required.append(param.name)

        return {
            "type": ToolSchemaConstants.TYPE_FUNCTION,
            ToolSchemaConstants.TYPE_FUNCTION: {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": ToolSchemaConstants.PARAM_TYPE_OBJECT,
                    "properties": properties,
                    "required": required,
                },
            },
        }

    def to_litellm_format(self) -> dict[str, Any]:
        """转换为LiteLLM工具格式(与OpenAI相同)"""
        return self.to_openai_format()


# 工具函数类型
ToolFunction = Callable[..., Any]

# ToolResult 现在从 tool_manager 导入（统一版本）


# 尝试导入 Annotated，如果不支持则提供兼容方案
try:
    from typing import Annotated

    HAS_ANNOTATED = True
except ImportError:
    try:
        from typing import Annotated

        HAS_ANNOTATED = True
    except ImportError:
        HAS_ANNOTATED = False
        Annotated = None


# 尝试导入 Literal
try:
    from typing import Literal
    from typing import get_args as get_literal_args

    HAS_LITERAL = True
except ImportError:
    try:
        from typing import Literal
        from typing import get_args as get_literal_args

        HAS_LITERAL = True
    except ImportError:
        HAS_LITERAL = False
        Literal = None


def _is_annotated_type(annotation: Any) -> bool:
    """检查是否是 Annotated 类型"""
    if not HAS_ANNOTATED:
        return False

    # 检查 __class__ 名称
    if hasattr(annotation, "__class__"):
        if "Annotated" in annotation.__class__.__name__:
            return True

    # 检查是否有 __metadata__ 属性
    if hasattr(annotation, "__metadata__"):
        return True

    return False


def _get_annotated_args(annotation: Any) -> tuple:
    """获取 Annotated 的参数"""
    if not HAS_ANNOTATED:
        return ()

    # 尝试 __args__
    args = getattr(annotation, "__args__", None)
    metadata = getattr(annotation, "__metadata__", None)

    if args and metadata:
        # Python 3.13+: __args__ 只包含实际类型，metadata 单独存储
        return args + metadata
    elif args and len(args) >= 2:
        # 旧版本: __args__ 包含类型和所有元数据
        return args
    elif metadata is not None:
        # 只有 metadata，尝试从 __origin__ 获取类型
        origin = getattr(annotation, "__origin__", None)
        if origin is not None:
            return (origin,) + tuple(metadata)

    return ()


def _extract_description_from_annotation(annotation: Any) -> str | None:
    """
    从注解中提取描述信息。

    支持:
    - Annotated[str, "描述信息"]
    - Annotated[str, Field(description="...")]

    Args:
        annotation: 类型注解

    Returns:
        描述字符串或 None
    """
    if not _is_annotated_type(annotation):
        return None

    args = _get_annotated_args(annotation)
    if len(args) < 2:
        return None

    # 第一个元素是实际类型，其余是元数据
    metadata = args[1:]

    for meta in metadata:
        # 直接是字符串描述
        if isinstance(meta, str):
            return meta

        # Pydantic Field 或其他有 description 的对象
        if hasattr(meta, "description") and meta.description:
            return str(meta.description)

        # dataclasses.Field
        if hasattr(meta, "metadata") and meta.metadata:
            for m in meta.metadata:
                if isinstance(m, str):
                    return m

    return None


def _get_base_type_from_annotation(annotation: Any) -> Any:
    """从 Annotated 获取基础类型"""
    if not _is_annotated_type(annotation):
        return annotation

    args = _get_annotated_args(annotation)
    if args:
        return args[0]

    return annotation


def _sanitize_default_value(default: Any) -> Any:
    """
    清理默认值，确保可以 JSON 序列化。

    处理以下情况:
    - Pydantic Field 对象 -> 提取 default 值
    - inspect.Parameter.empty -> None
    - 其他不可序列化对象 -> None
    """
    if default is inspect.Parameter.empty:
        return None

    # 检查是否是 Pydantic FieldInfo (Pydantic v2)
    if hasattr(default, "__class__"):
        class_name = default.__class__.__name__
        if "FieldInfo" in class_name or "ModelField" in class_name:
            # 尝试获取 default 属性
            if hasattr(default, "default") and default.default is not None:
                return default.default
            if hasattr(default, "default_factory") and default.default_factory is not None:
                try:
                    return default.default_factory()
                except:
                    return None
            return None

    # 检查基本类型
    if default is None or isinstance(default, (str, int, float, bool, list, dict)):
        return default

    # 尝试 JSON 序列化
    try:
        import json

        json.dumps(default)
        return default
    except (TypeError, ValueError):
        # 不可序列化，返回 None
        return None


def _is_pydantic_model(type_hint: Any) -> bool:
    """检查是否是 Pydantic BaseModel"""
    if not type_hint:
        return False

    # 检查是否是 Pydantic v1/v2 BaseModel
    try:
        from pydantic import BaseModel

        if isinstance(type_hint, type) and issubclass(type_hint, BaseModel):
            return True
    except ImportError:
        pass

    return False


def _is_typeddict(type_hint: Any) -> bool:
    """检查是否是 TypedDict"""
    if not type_hint:
        return False

    # 检查 __class__ 名称
    if hasattr(type_hint, "__class__"):
        if "TypedDict" in type_hint.__class__.__name__:
            return True

    # 检查是否是 typing.TypedDict 的子类
    origin = getattr(type_hint, "__origin__", None)
    if origin and "TypedDict" in str(origin):
        return True

    return False


def _is_literal_type(type_hint: Any) -> bool:
    """检查是否是 Literal 类型"""
    if not HAS_LITERAL:
        return False

    origin = getattr(type_hint, "__origin__", None)
    if origin is Literal:
        return True

    # 检查 __class__ 名称
    if hasattr(type_hint, "__class__"):
        if "Literal" in type_hint.__class__.__name__:
            return True

    return False


def _get_literal_values(type_hint: Any) -> list[str] | None:
    """获取 Literal 类型的所有可能值"""
    if not _is_literal_type(type_hint):
        return None

    args = getattr(type_hint, "__args__", ())
    return [str(arg) for arg in args]


def _parse_docstring_params(doc: str) -> dict[str, str]:
    """
        从 docstring 解析参数描述。

        支持多种格式:
        - Google Style: Args:
        name: description
        - NumPy Style: Parameters
    ----------
        name : type
            description
        - Sphinx Style: :param name: description

        Args:
            doc: 函数的 docstring

        Returns:
            参数名到描述的映射
    """
    param_docs = {}
    if not doc:
        return param_docs

    import re

    # 1. Sphinx style: :param name: description
    sphinx_pattern = r":param\s+(\w+)\s*:\s*(.+?)(?=:param|:return|:raises|:type|$)"
    for match in re.finditer(sphinx_pattern, doc, re.DOTALL):
        name = match.group(1)
        desc = " ".join(match.group(2).strip().split())
        param_docs[name] = desc

    # 2. Google style: Args:
    name: description
    args_match = re.search(
        r"(?:Args|Arguments|Params|Parameters)\s*:\s*\n(.*?)(?=\n\s*(?:Returns?|Yields?|Raises?|Example|Note|Attributes|\"\"\"|$))",
        doc,
        re.DOTALL,
    )
    if args_match:
        args_text = args_match.group(1)
        # Match "name: description" or "name (type): description"
        # 处理多行描述
        lines = args_text.split("\n")
        current_param = None
        current_desc = []

        for line in lines:
            stripped = line.strip()
            if not stripped:
                continue

            # 检查是否是新参数行 (name: description 或 name (type): description)
            param_match = re.match(r"(\w+)(?:\s*\([^)]*\))?\s*:\s*(.*)", stripped)
            if param_match and not stripped.startswith("-"):
                # 保存之前的参数
                if current_param and current_desc:
                    param_docs[current_param] = " ".join(current_desc)

                current_param = param_match.group(1)
                current_desc = [param_match.group(2)] if param_match.group(2) else []
            elif current_param and line.startswith(" " * 4):
                # 继续当前参数的描述（缩进4个空格以上）
                current_desc.append(stripped)

        # 保存最后一个参数
        if current_param and current_desc:
            param_docs[current_param] = " ".join(current_desc)

    return param_docs


def _type_hint_to_param_type(type_hint: Any) -> ToolParameterType:
    """将类型提示转换为 ToolParameterType"""
    # 处理 Annotated 类型
    base_type = _get_base_type_from_annotation(type_hint)

    # 处理 Literal 类型 -> 返回 STRING 并在外部处理 enum
    if _is_literal_type(base_type):
        return ToolParameterType.STRING

    # 处理 Pydantic Model -> OBJECT
    if _is_pydantic_model(base_type):
        return ToolParameterType.OBJECT

    # 处理 TypedDict -> OBJECT
    if _is_typeddict(base_type):
        return ToolParameterType.OBJECT

    # 处理 Optional 和 Union
    origin = getattr(base_type, "__origin__", None)
    args = getattr(base_type, "__args__", ())

    if origin is not None:
        # 处理 Optional[X] = Union[X, None]
        if origin is Union and len(args) == 2 and type(None) in args:
            # 获取非 None 的类型
            inner_type = args[0] if args[1] is type(None) else args[1]
            return _type_hint_to_param_type(inner_type)
        # 处理 List[X]
        elif origin in (list, list):
            return ToolParameterType.ARRAY
        # 处理 Dict[X, Y]
        elif origin in (dict, dict):
            return ToolParameterType.OBJECT

    # 基本类型
    if base_type == str:
        return ToolParameterType.STRING
    elif base_type == int:
        return ToolParameterType.INTEGER
    elif base_type == float:
        return ToolParameterType.NUMBER
    elif base_type == bool:
        return ToolParameterType.BOOLEAN
    elif base_type in (list, list):
        return ToolParameterType.ARRAY
    elif base_type in (dict, dict):
        return ToolParameterType.OBJECT

    return ToolParameterType.STRING  # 默认字符串


def _parse_pydantic_model(model_class: Any, param_name: str = "") -> ToolParameter:
    """
    从 Pydantic BaseModel 解析参数定义。

    Args:
        model_class: Pydantic BaseModel 类
        param_name: 参数名称

    Returns:
        ToolParameter 对象
    """
    try:
        from pydantic import BaseModel

        if not issubclass(model_class, BaseModel):
            return ToolParameter(
                name=param_name or "object",
                type=ToolParameterType.OBJECT,
                description=f"参数 {param_name}",
            )

        # 获取模型字段
        properties = {}
        required = []

        # Pydantic v2
        if hasattr(model_class, "model_fields"):
            fields = model_class.model_fields
            for field_name, field_info in fields.items():
                field_type = field_info.annotation
                field_desc = field_info.description or f"字段 {field_name}"
                field_default = field_info.default

                # 递归解析字段类型
                field_param = _parse_complex_type(field_type, field_name, field_desc, field_default)
                properties[field_name] = field_param

                # 检查是否必需
                if field_info.is_required():
                    required.append(field_name)

        # Pydantic v1
        elif hasattr(model_class, "__fields__"):
            fields = model_class.__fields__
            for field_name, field_info in fields.items():
                field_type = field_info.outer_type_
                field_desc = field_info.field_info.description or f"字段 {field_name}"
                field_default = field_info.default

                field_param = _parse_complex_type(field_type, field_name, field_desc, field_default)
                properties[field_name] = field_param

                if field_info.required:
                    required.append(field_name)

        return ToolParameter(
            name=param_name or model_class.__name__,
            type=ToolParameterType.OBJECT,
            description=model_class.__doc__ or f"参数 {param_name}",
            properties=properties,
        )

    except ImportError:
        return ToolParameter(
            name=param_name or "object",
            type=ToolParameterType.OBJECT,
            description=f"参数 {param_name}",
        )


def _parse_complex_type(
    type_hint: Any, name: str, description: str = "", default: Any = None
) -> ToolParameter:
    """
    解析复杂类型为 ToolParameter。

    支持:
    - 基本类型
    - List[T] - 数组
    - Dict[K, V] - 对象
    - Optional[T] - 可选类型
    - Literal[...] - 枚举
    - Pydantic BaseModel - 嵌套对象

    Args:
        type_hint: 类型注解
        name: 参数名称
        description: 参数描述
        default: 默认值

    Returns:
        ToolParameter 对象
    """
    # 清理默认值
    default = _sanitize_default_value(default)

    base_type = _get_base_type_from_annotation(type_hint)
    param_type = _type_hint_to_param_type(base_type)

    # 获取描述（优先从 Annotated）
    annotated_desc = _extract_description_from_annotation(type_hint)
    if annotated_desc:
        description = annotated_desc
    elif not description:
        type_name = _type_hint_to_param_type(base_type).value
        description = f"参数 {name} ({type_name})"

    # 检查是否必需
    origin = getattr(base_type, "__origin__", None)
    args = getattr(base_type, "__args__", ())
    is_optional = origin is Union and len(args) == 2 and type(None) in args
    required = default is None and not is_optional

    # 处理 Literal 类型 -> enum
    enum_values = None
    if _is_literal_type(base_type):
        enum_values = _get_literal_values(base_type)

    # 处理 List[T] -> items
    items = None
    if param_type == ToolParameterType.ARRAY and args:
        # 获取列表元素类型
        item_type = args[0]
        items = _parse_complex_type(item_type, "item", "列表元素")

    # 处理 Pydantic Model -> properties
    properties = None
    if _is_pydantic_model(base_type):
        pydantic_param = _parse_pydantic_model(base_type, name)
        properties = pydantic_param.properties

    return ToolParameter(
        name=name,
        type=param_type,
        description=description,
        required=required,
        default=default,
        enum=enum_values,
        items=items,
        properties=properties,
    )


def infer_parameters_from_function(func: Callable) -> list[ToolParameter]:
    """
    从函数签名、类型注解和 docstring 推断参数定义。

    支持复杂类型:
    - Annotated[str, "描述"] - 参数级别的注解
    - List[T] - 数组类型
    - Dict[K, V] - 字典类型
    - Optional[T] - 可选类型
    - Literal[...] - 枚举类型
    - Pydantic BaseModel - 嵌套对象

    优先级:
    1. Annotated[str, "描述"] - 参数级别的注解
    2. docstring Args 部分 - 函数文档中的参数描述
    3. 类型信息 - 基于类型生成默认描述

    Args:
        func: 要分析的函数

    Returns:
        参数定义列表

    Example:
        from typing import Annotated, List, Literal
        from pydantic import BaseModel

        class Address(BaseModel):
            street: str
            city: str

        def create_user(
            name: Annotated[str, "用户姓名"],
            age: int,
            tags: List[str] = None,
            status: Literal["active", "inactive"] = "active",
            address: Address = None,
        ) -> str:
            return f"Created {name}"

        params = infer_parameters_from_function(create_user)
    """
    sig = inspect.signature(func)
    params = []

    # 获取类型提示
    try:
        type_hints = get_type_hints(func)
    except Exception:
        type_hints = {}

    # 解析 docstring 中的参数描述
    doc = func.__doc__ or ""
    param_docs = _parse_docstring_params(doc)

    for name, param in sig.parameters.items():
        # 跳过 self/cls
        if name in ("self", "cls"):
            continue

        # 获取原始注解（优先使用 param.annotation，因为它保留 Annotated）
        raw_annotation = param.annotation
        type_hint = type_hints.get(name, raw_annotation)

        # 获取描述（优先级: Annotated > docstring > 默认）
        description = None

        # 1. 尝试从原始注解（Annotated）获取
        if raw_annotation != inspect.Parameter.empty:
            description = _extract_description_from_annotation(raw_annotation)

        # 2. 尝试从 docstring 获取
        if not description:
            description = param_docs.get(name)

        # 3. 使用默认描述（包含类型信息）
        if not description:
            type_name = (
                _type_hint_to_param_type(_get_base_type_from_annotation(type_hint)).value
                if type_hint != inspect.Parameter.empty
                else "string"
            )
            description = f"参数 {name} ({type_name})"

        # 清理默认值
        sanitized_default = _sanitize_default_value(param.default)
        is_required = param.default == inspect.Parameter.empty

        # 解析复杂类型
        if type_hint == inspect.Parameter.empty:
            # 无类型注解，使用字符串
            params.append(
                ToolParameter(
                    name=name,
                    type=ToolParameterType.STRING,
                    description=description,
                    required=is_required,
                    default=sanitized_default,
                )
            )
        else:
            # 使用复杂类型解析
            tool_param = _parse_complex_type(type_hint, name, description, sanitized_default)
            params.append(tool_param)

    return params
