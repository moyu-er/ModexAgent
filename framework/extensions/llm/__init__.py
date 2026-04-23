"""LLM Provider扩展

提供基于LiteLLM的LLM Provider实现,支持100+模型。
"""

try:
    from .litellm_provider import LiteLLMProvider
    __all__ = ["LiteLLMProvider"]
except ImportError:
    # LiteLLM未安装时提供提示
    __all__ = []
