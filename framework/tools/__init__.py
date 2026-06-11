"""工具系统 - 注册、执行和管理

支持:
- 全局注册表
- AOP拦截器(前置/后置/环绕钩子)
- 流式和非流式工具解析
- 批量工具执行
- 结果回填
- MCP 工具集成
"""

# 注册 - 从 registry.py 导入（唯一的注册表实现）
from .registry import ToolRegistry

# MCP 模块（框架内部实现）
try:
    from .mcp import (
        MCPClientManager,
        MCPConnectionError,
        MCPPromptTool,
        MCPResourceTool,
        MCPTool,
    )
except ImportError:
    MCPClientManager = None
    MCPConnectionError = None
    MCPTool = None
    MCPResourceTool = None
    MCPPromptTool = None

# MCP 适配器（兼容旧接口）
try:
    from .mcp_adapter import MCPToolAdapter, MCPToolRegistry
except ImportError:
    MCPToolAdapter = None
    MCPToolRegistry = None

