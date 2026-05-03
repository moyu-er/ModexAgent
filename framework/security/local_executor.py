"""本地安全执行器

无需 Sandbox，直接使用验证器和审批处理器进行安全控制。
适合本地开发环境或可信环境使用。

Example:
    from framework.security import (
        LocalSecureExecutor,
        SecurityPolicy,
        SecurityPolicyConfig,
        ToolSecurityConfig,
        CommandValidator,
        ConsoleApprovalHandler,
    )
    
    # 创建安全策略
    policy = SecurityPolicy(SecurityPolicyConfig(
        tool_configs=[
            ToolSecurityConfig(
                tool_pattern="shell_tool",
                validator=CommandValidator(),
                approval_handler=ConsoleApprovalHandler(),
            ),
            ToolSecurityConfig(
                tool_pattern="file_tool",
                validator=FilePathValidator(),
            ),
        ]
    ))
    
    # 创建安全执行器
    executor = LocalSecureExecutor(policy)
    
    # 执行工具（带安全校验和审批）
    result = await executor.execute("shell_tool", {"command": "ls -la"})
"""

from typing import Any

from .exceptions import SecurityError
from .policy import SecurityPolicy
from .validators import ValidationStatus


class LocalSecureExecutor:
    """本地安全执行器
    
    无需 Sandbox，直接使用安全策略进行验证和审批。
    
    执行流程：
    1. 使用 SecurityPolicy 验证工具调用
    2. 如果验证通过，直接执行
    3. 如果需要审批，调用 ApprovalHandler
    4. 如果验证失败，抛出 SecurityError
    
    Example:
        executor = LocalSecureExecutor(security_policy)
        
        # 执行工具（会自动进行安全校验）
        result = await executor.execute(
            tool_name="shell_tool",
            arguments={"command": "ls -la"}
        )
    """

    def __init__(self, security_policy: SecurityPolicy):
        """初始化本地安全执行器
        
        Args:
            security_policy: 安全策略配置
        """
        self._policy = security_policy

    async def validate(
        self,
        tool_name: str,
        arguments: dict[str, Any]
    ) -> tuple[bool, str | None]:
        """验证工具调用是否安全
        
        Args:
            tool_name: 工具名称
            arguments: 工具参数
            
        Returns:
            (是否允许执行, 拒绝/审批原因)
        """
        try:
            result = await self._policy.validate(tool_name, arguments)

            if result.status == ValidationStatus.VALID:
                return True, None
            elif result.status == ValidationStatus.INVALID:
                return False, f"Blocked: {result.reason}"
            elif result.status == ValidationStatus.SUSPICIOUS:
                # 需要审批
                approved = await self._policy.approve(tool_name, arguments, result)
                if approved:
                    return True, None
                else:
                    return False, f"Denied: {result.reason}"

            return False, "Unknown validation status"

        except Exception as e:
            return False, f"Validation error: {e}"

    async def execute(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        actual_executor: Any | None = None
    ) -> Any:
        """安全执行工具
        
        Args:
            tool_name: 工具名称
            arguments: 工具参数
            actual_executor: 实际执行工具的对象（如 ToolRegistry 或 Tool）
            
        Returns:
            工具执行结果
            
        Raises:
            SecurityError: 安全校验失败或审批被拒绝
        """
        # 1. 验证
        allowed, reason = await self.validate(tool_name, arguments)

        if not allowed:
            raise SecurityError(f"Tool '{tool_name}' execution blocked: {reason}")

        # 2. 执行（调用实际执行器）
        if actual_executor is None:
            raise ValueError("actual_executor is required for execution")

        # 支持多种执行器类型
        if hasattr(actual_executor, 'execute'):
            # Tool 对象
            return await actual_executor.execute(**arguments)
        elif hasattr(actual_executor, 'call_tool'):
            # ToolRegistry
            return await actual_executor.call_tool(tool_name, arguments)
        elif callable(actual_executor):
            # 可调用对象
            return await actual_executor(tool_name, arguments)
        else:
            raise ValueError(f"Unsupported executor type: {type(actual_executor)}")


class LocalSecureToolWrapper:
    """本地安全工具包装器
    
    包装一个 Tool，在执行前自动进行安全校验和审批。
    
    Example:
        from framework.tools.standard import ShellTool
        
        # 创建基础工具
        shell_tool = ShellTool()
        
        # 包装为安全工具
        secure_shell = LocalSecureToolWrapper(
            tool=shell_tool,
            security_policy=policy
        )
        
        # 执行（自动安全校验）
        result = await secure_shell.execute(command="ls -la")
    """

    def __init__(
        self,
        tool: Any,
        security_policy: SecurityPolicy,
        auto_approve_low_risk: bool = True
    ):
        """初始化安全工具包装器
        
        Args:
            tool: 原始工具对象（必须有 name, execute 属性）
            security_policy: 安全策略
            auto_approve_low_risk: 是否自动批准低风险操作
        """
        self._tool = tool
        self._policy = security_policy
        self._auto_approve_low_risk = auto_approve_low_risk
        self._executor = LocalSecureExecutor(security_policy)

    @property
    def name(self) -> str:
        """工具名称"""
        return self._tool.name

    @property
    def description(self) -> str:
        """工具描述"""
        return getattr(self._tool, 'description', '')

    @property
    def parameters(self) -> dict[str, Any]:
        """工具参数定义"""
        return getattr(self._tool, 'parameters', {})

    async def execute(self, **kwargs) -> Any:
        """安全执行工具
        
        Args:
            **kwargs: 工具参数
            
        Returns:
            工具执行结果
        """
        return await self._executor.execute(
            tool_name=self._tool.name,
            arguments=kwargs,
            actual_executor=self._tool
        )
