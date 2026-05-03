"""工具元数据解析器

从函数和类自动解析工具元数据，支持多种文档字符串格式。
"""

import inspect
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, get_type_hints


@dataclass
class ParsedMetadata:
    """解析后的元数据"""
    name: str
    description: str
    parameters: dict[str, "ParsedParameter"]
    tags: set[str] = field(default_factory=set)
    category: str = "general"
    examples: list[str] = field(default_factory=list)
    notes: str | None = None
    deprecated: bool = False
    deprecated_reason: str | None = None


@dataclass
class ParsedParameter:
    """解析后的参数信息"""
    name: str
    type_str: str
    description: str = ""
    default: Any = None
    required: bool = True
    examples: list[str] = field(default_factory=list)


class DocstringParser:
    """文档字符串解析器"""

    @staticmethod
    def parse(func: Callable) -> ParsedMetadata:
        """
        解析函数的文档字符串。
        
        支持多种格式:
        - Google Style
        - NumPy Style
        - Sphinx/RestructuredText Style
        - 简单描述
        
        Args:
            func: 要解析的函数
        
        Returns:
            解析后的元数据
        """
        name = func.__name__
        doc = func.__doc__ or ""

        # 解析主描述
        description = DocstringParser._extract_description(doc)

        # 解析参数
        parameters = DocstringParser._extract_parameters(func, doc)

        # 解析其他元数据
        tags = DocstringParser._extract_tags(doc)
        category = DocstringParser._extract_category(doc)
        examples = DocstringParser._extract_examples(doc)
        notes = DocstringParser._extract_notes(doc)
        deprecated, deprecated_reason = DocstringParser._extract_deprecated(doc)

        return ParsedMetadata(
            name=name,
            description=description,
            parameters=parameters,
            tags=tags,
            category=category,
            examples=examples,
            notes=notes,
            deprecated=deprecated,
            deprecated_reason=deprecated_reason,
        )

    @staticmethod
    def _extract_description(doc: str) -> str:
        """提取主描述(第一段)"""
        if not doc:
            return ""

        # 找到第一个空行或特殊标记之前的内容
        lines = doc.strip().split("\n")
        description_lines = []

        for line in lines:
            stripped = line.strip()
            # 遇到参数、返回值等标记停止
            if stripped.startswith((":", "Args:", "Arguments:", "Parameters:",
                                   "Returns:", "Yields:", "Raises:", "Example:",
                                   "Examples:", "Note:", "Notes:", "Tags:",
                                   "Category:", "Deprecated:")):
                break
            description_lines.append(stripped)

        return " ".join(description_lines).strip()

    @staticmethod
    def _extract_parameters(func: Callable, doc: str) -> dict[str, ParsedParameter]:
        """提取参数信息"""
        parameters = {}
        sig = inspect.signature(func)
        type_hints = get_type_hints(func)

        # 首先获取函数签名中的参数信息
        for param_name, param in sig.parameters.items():
            if param_name == "self":
                continue

            param_type = type_hints.get(param_name, Any)
            type_str = DocstringParser._type_to_string(param_type)

            parameters[param_name] = ParsedParameter(
                name=param_name,
                type_str=type_str,
                default=param.default if param.default is not inspect.Parameter.empty else None,
                required=param.default is inspect.Parameter.empty,
            )

        # 然后从docstring中提取描述
        param_docs = DocstringParser._parse_param_docs(doc)
        for name, desc in param_docs.items():
            if name in parameters:
                parameters[name].description = desc

        return parameters

    @staticmethod
    def _parse_param_docs(doc: str) -> dict[str, str]:
        """解析参数文档"""
        param_docs = {}

        if not doc:
            return param_docs

        # Google Style: "name: description"
        # NumPy Style: "name : type\n    description"
        # Sphinx Style: ":param name: description"

        # Sphinx style
        sphinx_pattern = r":param\s+(\w+)\s*:\s*(.+?)(?=:param|:return|:raises|$)"
        for match in re.finditer(sphinx_pattern, doc, re.DOTALL):
            name = match.group(1)
            desc = match.group(2).strip().replace("\n", " ")
            param_docs[name] = desc

        # Google style (in Args section)
        args_match = re.search(r"Args:\s*(.+?)(?=\n\s*\w+:|$)", doc, re.DOTALL)
        if args_match:
            args_text = args_match.group(1)
            # Match "name: description" or "name (type): description"
            google_pattern = r"\n\s+(\w+)(?:\s*\([^)]*\))?\s*:\s*(.+?)(?=\n\s+\w+:|\Z)"
            for match in re.finditer(google_pattern, args_text, re.DOTALL):
                name = match.group(1)
                desc = match.group(2).strip().replace("\n", " ")
                if name not in param_docs:
                    param_docs[name] = desc

        return param_docs

    @staticmethod
    def _extract_tags(doc: str) -> set[str]:
        """提取标签"""
        tags = set()

        # 查找 Tags: tag1, tag2, tag3
        match = re.search(r"[Tt]ags?\s*:\s*(.+?)(?=\n\s*\w+:|$)", doc)
        if match:
            tag_text = match.group(1)
            tags.update(t.strip() for t in tag_text.split(",") if t.strip())

        return tags

    @staticmethod
    def _extract_category(doc: str) -> str:
        """提取类别"""
        match = re.search(r"[Cc]ategor(?:y|ies)\s*:\s*(\w+)", doc)
        if match:
            return match.group(1).lower()
        return "general"

    @staticmethod
    def _extract_examples(doc: str) -> list[str]:
        """提取示例"""
        examples = []

        # 查找 Example: 或 Examples: 部分
        pattern = r"[Ee]xamples?\s*:\s*(.+?)(?=\n\s*\w+:|$)"
        matches = re.findall(pattern, doc, re.DOTALL)

        for match in matches:
            # 分割多个示例
            example_texts = re.split(r"\n\s*>>>\s*", match.strip())
            for ex in example_texts:
                ex = ex.strip()
                if ex:
                    examples.append(ex)

        return examples

    @staticmethod
    def _extract_notes(doc: str) -> str | None:
        """提取注意事项"""
        # 查找 Note: 或 Notes: 部分
        match = re.search(r"[Nn]otes?\s*:\s*(.+?)(?=\n\s*\w+:|$)", doc, re.DOTALL)
        if match:
            return match.group(1).strip()
        return None

    @staticmethod
    def _extract_deprecated(doc: str) -> tuple[bool, str | None]:
        """提取弃用信息"""
        if re.search(r"\.\.\s*deprecated::", doc) or "[Deprecated]" in doc:
            # 查找弃用原因
            match = re.search(r"[Dd]eprecated\s*:\s*(.+?)(?=\n\s*\w+:|$)", doc, re.DOTALL)
            reason = match.group(1).strip() if match else None
            return True, reason
        return False, None

    @staticmethod
    def _type_to_string(type_hint: Any) -> str:
        """将类型提示转换为字符串"""
        if hasattr(type_hint, "__name__"):
            return type_hint.__name__
        return str(type_hint).replace("<class '", "").replace("'>", "")


def parse_function_metadata(func: Callable) -> ParsedMetadata:
    """
    解析函数的元数据。
    
    便捷函数，使用 DocstringParser。
    
    Args:
        func: 要解析的函数
    
    Returns:
        解析后的元数据
    
    Example:
        def get_weather(location: str, unit: str = "celsius") -> dict:
            \"\"\"
            获取指定位置的天气信息。
            
            这是一个简单的天气查询工具，支持摄氏度/华氏度。
            
            Args:
                location: 城市名称，例如 "北京"
                unit: 温度单位，"celsius" 或 "fahrenheit"
            
            Returns:
                包含天气信息的字典
            
            Tags: weather, external-api
            Category: utility
            \"\"\"
            return {"temp": 25, "unit": unit}
        
        metadata = parse_function_metadata(get_weather)
        print(metadata.description)  # "获取指定位置的天气信息。"
        print(metadata.tags)  # {"weather", "external-api"}
    """
    return DocstringParser.parse(func)


def extract_tool_info_from_source(func: Callable) -> dict[str, Any]:
    """
    从函数源码提取完整的工具信息。
    
    包括函数签名、文档字符串、类型注解等。
    
    Args:
        func: 要分析的函数
    
    Returns:
        包含所有工具信息的字典
    """
    metadata = parse_function_metadata(func)

    return {
        "name": metadata.name,
        "description": metadata.description,
        "parameters": {
            name: {
                "type": param.type_str,
                "description": param.description,
                "required": param.required,
                "default": param.default,
            }
            for name, param in metadata.parameters.items()
        },
        "tags": list(metadata.tags),
        "category": metadata.category,
        "examples": metadata.examples,
        "notes": metadata.notes,
        "deprecated": metadata.deprecated,
        "deprecated_reason": metadata.deprecated_reason,
    }
