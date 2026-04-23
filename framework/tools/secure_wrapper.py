"""Secure tool wrapper with validation and approval.

This module provides SecureToolWrapper that integrates ToolValidator
with ApprovalHandler for complete tool security.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .toolkit import ToolWrapper
    from .types import ToolResult
    from ..security import (
        ApprovalHandler,
        ToolValidator,
        RiskLevel,
        DefaultAction,
        SecurityPolicy,
    )


@dataclass
class SecureToolConfig:
    """Configuration for secure tool execution.
    
    Attributes:
        validator: Tool validator for checking tool arguments
        approval_handler: Handler for approval decisions
        risk_policies: Mapping from risk level to default action
        default_action: Fallback action when no policy applies
        security_policy: Unified security policy (alternative to separate validator/handler)
    
    Example:
        # Using separate validator and handler
        config = SecureToolConfig(
            validator=CommandValidator(),
            approval_handler=ConsoleApprovalHandler(),
            risk_policies={
                RiskLevel.LOW: DefaultAction.ALLOW,
                RiskLevel.MEDIUM: DefaultAction.ASK,
                RiskLevel.HIGH: DefaultAction.ASK,
                RiskLevel.CRITICAL: DefaultAction.DENY,
            }
        )
        
        # Using unified security policy (recommended)
        config = SecureToolConfig(
            security_policy=SecurityPolicy(...)
        )
    """
    
    validator: Optional["ToolValidator"] = None
    """Validator for this tool. If None, uses security_policy or allows all."""
    
    approval_handler: Optional["ApprovalHandler"] = None
    """Approval handler for this tool. If None, uses security_policy."""
    
    risk_policies: Dict["RiskLevel", "DefaultAction"] = field(default_factory=dict)
    """Risk level to action mapping. Only used with separate validator/handler."""
    
    default_action: "DefaultAction" = None  # Will be set to ASK in post_init
    """Default action when no policy applies."""
    
    security_policy: Optional["SecurityPolicy"] = None
    """Unified security policy. If set, uses this instead of separate validator/handler."""
    
    def __post_init__(self):
        if self.default_action is None:
            from ..security import DefaultAction
            self.default_action = DefaultAction.ASK
        
        # Set default risk policies if not provided and not using security_policy
        if not self.risk_policies and not self.security_policy:
            from ..security import RiskLevel, DefaultAction
            self.risk_policies = {
                RiskLevel.LOW: DefaultAction.ALLOW,
                RiskLevel.MEDIUM: DefaultAction.ASK,
                RiskLevel.HIGH: DefaultAction.ASK,
                RiskLevel.CRITICAL: DefaultAction.DENY,
            }


class SecureToolWrapper:
    """Wrapper that adds security validation and approval to tools.
    
    Implements a two-phase security model:
    1. Validation: Check tool arguments for dangerous patterns
    2. Approval: Get approval for suspicious operations
    
    Can use either:
    - Separate validator and approval handler (legacy mode)
    - Unified SecurityPolicy (recommended)
    
    Example:
        # Using unified security policy (recommended)
        from framework.security import SecurityPolicy, SecurityPolicyConfig
        
        policy = SecurityPolicy(SecurityPolicyConfig(...))
        secure_tool = SecureToolWrapper(
            tool_wrapper=bash_tool,
            config=SecureToolConfig(security_policy=policy)
        )
        
        # Execute with automatic security checks
        result = await secure_tool.execute(command="ls -la")
        # If command is safe: executes immediately
        # If command is suspicious: prompts for approval
        # If command is dangerous: returns error
    """
    
    def __init__(self, tool_wrapper: "ToolWrapper", config: SecureToolConfig):
        """Initialize secure tool wrapper.
        
        Args:
            tool_wrapper: The tool wrapper to secure
            config: Security configuration
        """
        self._tool = tool_wrapper
        self._config = config
    
    @property
    def name(self) -> str:
        """Get the tool name."""
        return self._tool.name
    
    @property
    def metadata(self):
        """Get the tool metadata."""
        return self._tool.metadata
    
    async def execute(self, **kwargs) -> "ToolResult":
        """Execute the tool with security checks.
        
        Implements two-phase security:
        1. Validation phase: Check arguments for dangerous patterns
        2. Approval phase: Get approval if validation is suspicious
        
        Args:
            **kwargs: Tool arguments
            
        Returns:
            ToolResult with execution results or error
        """
        from .types import ToolResult
        
        # Use unified security policy if available
        if self._config.security_policy:
            result = await self._config.security_policy.check(self._tool.name, kwargs)
            
            if not result.allowed:
                return ToolResult(
                    tool_name=self._tool.name,
                    success=False,
                    error=result.reason,
                    result=None,
                )
            
            # Approved, execute the tool
            return await self._tool.execute(**kwargs)
        
        # Legacy mode: use separate validator and handler
        from ..security import ValidationStatus, DefaultAction
        
        # Phase 1: Validation
        if self._config.validator:
            validation = await self._config.validator.validate(self._tool.name, kwargs)
            
            # Handle INVALID: Block immediately
            if validation.status == ValidationStatus.INVALID:
                return ToolResult(
                    tool_name=self._tool.name,
                    success=False,
                    error=f"Security validation failed: {validation.reason}",
                    result=None,
                )
            
            # Handle SUSPICIOUS: Enter approval phase
            if validation.status == ValidationStatus.SUSPICIOUS:
                # Look up policy for this risk level
                action = self._config.risk_policies.get(
                    validation.risk_level, 
                    self._config.default_action
                )
                
                # DENY: Block without approval
                if action == DefaultAction.DENY:
                    return ToolResult(
                        tool_name=self._tool.name,
                        success=False,
                        error=f"Blocked: {validation.reason}",
                        result=None,
                    )
                
                # ASK: Require approval
                if action == DefaultAction.ASK:
                    if not self._config.approval_handler:
                        return ToolResult(
                            tool_name=self._tool.name,
                            success=False,
                            error=f"Approval required but no handler configured: {validation.reason}",
                            result=None,
                        )
                    
                    # Call approval handler
                    approved = await self._config.approval_handler.approve(
                        command=f"{self._tool.name}({kwargs})",
                        reason=validation.reason
                    )
                    
                    if not approved:
                        return ToolResult(
                            tool_name=self._tool.name,
                            success=False,
                            error="Approval denied",
                            result=None,
                        )
                
                # ALLOW: Continue to execution (fall through)
        
        # Execute the tool
        return await self._tool.execute(**kwargs)


class SecureToolExecutor:
    """Tool executor that automatically applies security policies.
    
    This executor wraps all tools with SecureToolWrapper automatically,
    using a unified SecurityPolicy for consistent security across all tools.
    
    Example:
        from framework.security import SecurityPolicy, SecurityPolicyConfig
        from framework.tools.secure_wrapper import SecureToolExecutor
        
        # Create security policy
        policy = SecurityPolicy(SecurityPolicyConfig(
            tool_configs=[
                ToolSecurityConfig(tool_pattern="bash", validator=CommandValidator()),
                ToolSecurityConfig(tool_pattern="file_*", validator=FilePathValidator()),
            ]
        ))
        
        # Create secure executor
        executor = SecureToolExecutor(policy)
        
        # Register tools (automatically wrapped with security)
        executor.register_tool(bash_tool)
        executor.register_tool(file_read_tool)
        
        # Execute with automatic security checks
        result = await executor.execute("bash", command="ls -la")
    """
    
    def __init__(self, security_policy: "SecurityPolicy"):
        """Initialize secure tool executor.
        
        Args:
            security_policy: Security policy to apply to all tools
        """
        self._policy = security_policy
        self._tools: Dict[str, SecureToolWrapper] = {}
    
    def register_tool(self, tool_wrapper: "ToolWrapper") -> "SecureToolExecutor":
        """Register a tool with security wrapping.
        
        Args:
            tool_wrapper: The tool to register
            
        Returns:
            Self for chaining
        """
        config = SecureToolConfig(security_policy=self._policy)
        secure_tool = SecureToolWrapper(tool_wrapper, config)
        self._tools[tool_wrapper.name] = secure_tool
        return self
    
    def unregister_tool(self, tool_name: str) -> "SecureToolExecutor":
        """Unregister a tool.
        
        Args:
            tool_name: Name of the tool to unregister
            
        Returns:
            Self for chaining
        """
        if tool_name in self._tools:
            del self._tools[tool_name]
        return self
    
    async def execute(self, tool_name: str, **kwargs) -> "ToolResult":
        """Execute a tool with security checks.
        
        Args:
            tool_name: Name of the tool to execute
            **kwargs: Tool arguments
            
        Returns:
            ToolResult with execution results
            
        Raises:
            KeyError: If tool is not registered
        """
        if tool_name not in self._tools:
            from .types import ToolResult
            return ToolResult(
                tool_name=tool_name,
                success=False,
                error=f"Tool '{tool_name}' not found",
                result=None,
            )
        
        return await self._tools[tool_name].execute(**kwargs)
    
    def get_tool(self, tool_name: str) -> Optional[SecureToolWrapper]:
        """Get a registered secure tool.
        
        Args:
            tool_name: Name of the tool
            
        Returns:
            SecureToolWrapper or None if not found
        """
        return self._tools.get(tool_name)
    
    def list_tools(self) -> list:
        """List all registered tool names.
        
        Returns:
            List of tool names
        """
        return list(self._tools.keys())
