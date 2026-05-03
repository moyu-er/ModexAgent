"""Security module for command execution approval and policy enforcement.

This module provides a flexible security framework for command execution
with support for multiple approval mechanisms and a three-level permission model.

Example:
    from framework.security import (
        SecurityPolicy,
        SecurityPolicyConfig,
        ToolSecurityConfig,
        ConsoleApprovalHandler,
        CommandValidator,
    )

    # Configure security policy
    policy = SecurityPolicy(SecurityPolicyConfig(
        tool_configs=[
            ToolSecurityConfig(
                tool_pattern="bash",
                validator=CommandValidator(),
            ),
        ]
    ))

    # Check tool execution
    result = await policy.check("bash", {"command": "ls -la"})
    if result.allowed:
        print("Command approved!")
    else:
        print(f"Command rejected: {result.reason}")
"""

from .exceptions import ApprovalTimeoutError, CommandRejectedError, SecurityError
from .handlers import (
    APIBasedApprovalHandler,
    ApprovalHandler,
    CompositeApprovalHandler,
    ConfigBasedApprovalHandler,
    ConsoleApprovalHandler,
    LoggingApprovalHandler,
)
from .local_executor import (
    LocalSecureExecutor,
    LocalSecureToolWrapper,
)
from .policy import (
    DEFAULT_ALLOW_PATTERNS,
    DEFAULT_ASK_PATTERNS,
    DEFAULT_DENY_PATTERNS,
    # Legacy (for backward compatibility)
    CommandPolicy,
    SecurityChecker,
    SecurityCheckResult,
    SecurityConfig,
    # New unified policy system
    SecurityPolicy,
    SecurityPolicyConfig,
    ToolSecurityConfig,
    create_security_policy,
)
from .validators import (
    CommandValidator,
    CompositeValidator,
    DefaultAction,
    FilePathValidator,
    FunctionValidator,
    ParameterValidator,
    RiskLevel,
    # Validators
    ToolValidator,
    # Data classes
    ValidationResult,
    # Enums
    ValidationStatus,
)

__all__ = [
    # Exceptions
    "SecurityError",
    "CommandRejectedError",
    "ApprovalTimeoutError",
    # Handlers
    "ApprovalHandler",
    "ConsoleApprovalHandler",
    "ConfigBasedApprovalHandler",
    "APIBasedApprovalHandler",
    "CompositeApprovalHandler",
    "LoggingApprovalHandler",
    # New Policy System
    "SecurityPolicy",
    "SecurityPolicyConfig",
    "ToolSecurityConfig",
    "SecurityCheckResult",
    "create_security_policy",
    # Legacy Policy (backward compatibility)
    "CommandPolicy",
    "SecurityConfig",
    "SecurityChecker",
    "DEFAULT_ALLOW_PATTERNS",
    "DEFAULT_DENY_PATTERNS",
    "DEFAULT_ASK_PATTERNS",
    # Validators - Enums
    "ValidationStatus",
    "RiskLevel",
    "DefaultAction",
    # Validators - Data classes
    "ValidationResult",
    # Validators - Classes
    "ToolValidator",
    "CommandValidator",
    "FilePathValidator",
    "CompositeValidator",
    "FunctionValidator",
    "ParameterValidator",
    # Local Executor (No Sandbox)
    "LocalSecureExecutor",
    "LocalSecureToolWrapper",
]
