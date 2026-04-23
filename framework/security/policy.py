"""Security policy for unified validation and approval.

This module provides SecurityPolicy which unifies tool validation and approval
into a single configurable system. It supports per-tool configuration with
pattern matching (including * wildcards).
"""

import re
import fnmatch
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Callable, Pattern

from .validators import (
    ValidationResult,
    ValidationStatus,
    RiskLevel,
    DefaultAction,
    ToolValidator,
    CommandValidator,
    FilePathValidator,
)
from .handlers import ApprovalHandler, ConfigBasedApprovalHandler
from .exceptions import SecurityError


@dataclass
class ToolSecurityConfig:
    """Security configuration for a specific tool or tool pattern.
    
    This allows per-tool customization of security policies.
    Supports pattern matching with * wildcards.
    
    Example:
        # Exact match for 'bash' tool
        ToolSecurityConfig(
            tool_pattern="bash",
            validator=CommandValidator(dangerous_patterns=[...]),
            risk_policies={RiskLevel.HIGH: DefaultAction.DENY},
        )
        
        # Wildcard match for all file operations
        ToolSecurityConfig(
            tool_pattern="file_*",
            validator=FilePathValidator(allowed_paths=["/tmp"]),
        )
        
        # Default config for all tools
        ToolSecurityConfig(
            tool_pattern="*",
            validator=CompositeValidator([...]),
        )
    """
    
    tool_pattern: str = "*"
    """Tool name pattern (supports * wildcards, e.g., 'bash', 'file_*', '*')"""
    
    validator: Optional[ToolValidator] = None
    """Validator for this tool. If None, uses global default."""
    
    approval_handler: Optional[ApprovalHandler] = None
    """Approval handler for this tool. If None, uses global default."""
    
    risk_policies: Dict[RiskLevel, DefaultAction] = field(default_factory=dict)
    """Risk level to action mapping. Empty means use global default."""
    
    enabled: bool = True
    """Whether this config is active."""
    
    priority: int = 0
    """Priority for matching (higher = checked first). Exact matches should have higher priority."""
    
    def matches(self, tool_name: str) -> bool:
        """Check if this config matches a tool name.
        
        Uses fnmatch for Unix shell-style wildcards.
        
        Args:
            tool_name: The tool name to match against
            
        Returns:
            True if pattern matches
        """
        return fnmatch.fnmatch(tool_name, self.tool_pattern)


@dataclass
class SecurityPolicyConfig:
    """Configuration for SecurityPolicy.
    
    Provides global defaults and per-tool configurations.
    
    Example:
        config = SecurityPolicyConfig(
            # Global defaults
            default_validator=CommandValidator(),
            default_approval_handler=ConsoleApprovalHandler(),
            default_risk_policies={
                RiskLevel.LOW: DefaultAction.ALLOW,
                RiskLevel.MEDIUM: DefaultAction.ASK,
                RiskLevel.HIGH: DefaultAction.ASK,
                RiskLevel.CRITICAL: DefaultAction.DENY,
            },
            # Per-tool configs
            tool_configs=[
                ToolSecurityConfig(
                    tool_pattern="bash",
                    validator=CommandValidator(dangerous_patterns=[...]),
                ),
                ToolSecurityConfig(
                    tool_pattern="file_*",
                    validator=FilePathValidator(allowed_paths=["/tmp"]),
                ),
            ]
        )
    """
    
    # Global defaults
    default_validator: Optional[ToolValidator] = None
    default_approval_handler: Optional[ApprovalHandler] = None
    default_risk_policies: Dict[RiskLevel, DefaultAction] = field(default_factory=lambda: {
        RiskLevel.LOW: DefaultAction.ALLOW,
        RiskLevel.MEDIUM: DefaultAction.ASK,
        RiskLevel.HIGH: DefaultAction.ASK,
        RiskLevel.CRITICAL: DefaultAction.DENY,
    })
    default_action: DefaultAction = DefaultAction.ASK
    
    # Per-tool configurations (checked in priority order)
    tool_configs: List[ToolSecurityConfig] = field(default_factory=list)
    
    # Whether to allow execution if no validator is configured
    allow_without_validator: bool = True
    
    def __post_init__(self):
        # Sort tool configs by priority (descending)
        self.tool_configs.sort(key=lambda c: c.priority, reverse=True)


class SecurityPolicy:
    """Unified security policy combining validation and approval.
    
    This is the main entry point for tool security. It provides:
    1. Per-tool configuration with pattern matching
    2. Unified validation + approval flow
    3. Configurable risk policies
    4. Extensible validator and handler system
    
    The policy works as follows:
    1. Find matching ToolSecurityConfig for the tool name
    2. Run validation (if configured)
    3. Based on validation result and risk policies, decide action
    4. If approval needed, run approval handler
    5. Return result
    
    Example:
        # Create policy with configuration
        policy = SecurityPolicy(SecurityPolicyConfig(
            tool_configs=[
                ToolSecurityConfig(
                    tool_pattern="bash",
                    validator=CommandValidator(),
                    risk_policies={RiskLevel.HIGH: DefaultAction.DENY},
                ),
                ToolSecurityConfig(
                    tool_pattern="*",
                    validator=FilePathValidator(),
                ),
            ]
        ))
        
        # Check a tool execution
        result = await policy.check("bash", {"command": "rm -rf /"})
        if result.allowed:
            await execute_tool()
    """
    
    def __init__(self, config: Optional[SecurityPolicyConfig] = None):
        """Initialize security policy.
        
        Args:
            config: Policy configuration. If None, uses permissive defaults.
        """
        self._config = config or SecurityPolicyConfig()
    
    async def check(self, tool_name: str, arguments: Dict[str, Any]) -> "SecurityCheckResult":
        """Check if a tool execution is allowed.
        
        This is the main entry point. It performs:
        1. Find matching config for the tool
        2. Validate arguments
        3. Apply risk policies
        4. Get approval if needed
        
        Args:
            tool_name: Name of the tool being invoked
            arguments: Tool arguments
            
        Returns:
            SecurityCheckResult with allowed status and details
        """
        # Find matching tool config
        tool_config = self._find_tool_config(tool_name)
        
        # Get validator (tool-specific or global)
        validator = self._get_validator(tool_config)
        
        # Phase 1: Validation
        if validator:
            validation = await validator.validate(tool_name, arguments)

            # Handle INVALID: Block immediately
            if validation.status == ValidationStatus.INVALID:
                return SecurityCheckResult(
                    allowed=False,
                    tool_name=tool_name,
                    arguments=arguments,
                    validation_result=validation,
                    reason=f"Validation failed: {validation.reason}",
                )

            # Handle SUSPICIOUS: Apply risk policies
            if validation.status == ValidationStatus.SUSPICIOUS:
                action = self._get_action_for_risk(validation.risk_level, tool_config)

                # DENY: Block without approval
                if action == DefaultAction.DENY:
                    return SecurityCheckResult(
                        allowed=False,
                        tool_name=tool_name,
                        arguments=arguments,
                        validation_result=validation,
                        reason=f"Blocked by policy: {validation.reason}",
                    )

                # ASK: Require approval
                if action == DefaultAction.ASK:
                    approval_handler = self._get_approval_handler(tool_config)

                    if not approval_handler:
                        return SecurityCheckResult(
                            allowed=False,
                            tool_name=tool_name,
                            arguments=arguments,
                            validation_result=validation,
                            reason="Approval required but no handler configured",
                        )

                    # Call approval handler
                    approved = await approval_handler.approve(
                        command=f"{tool_name}({arguments})",
                        reason=validation.reason,
                    )

                    if not approved:
                        return SecurityCheckResult(
                            allowed=False,
                            tool_name=tool_name,
                            arguments=arguments,
                            validation_result=validation,
                            reason="Approval denied",
                        )

                # ALLOW: Continue (fall through)

            # VALID: Continue (fall through)
        else:
            # No validator configured - check allow_without_validator setting
            if not self._config.allow_without_validator:
                return SecurityCheckResult(
                    allowed=False,
                    tool_name=tool_name,
                    arguments=arguments,
                    validation_result=None,
                    reason="No validator configured and allow_without_validator is False",
                )

        # Execution allowed
        return SecurityCheckResult(
            allowed=True,
            tool_name=tool_name,
            arguments=arguments,
            validation_result=None,
            reason="Execution approved",
        )
    
    def _find_tool_config(self, tool_name: str) -> Optional[ToolSecurityConfig]:
        """Find the best matching tool config for a tool name.
        
        Checks configs in priority order and returns the first match.
        
        Args:
            tool_name: The tool name to find config for
            
        Returns:
            Matching ToolSecurityConfig or None
        """
        for config in self._config.tool_configs:
            if config.enabled and config.matches(tool_name):
                return config
        return None
    
    def _get_validator(self, tool_config: Optional[ToolSecurityConfig]) -> Optional[ToolValidator]:
        """Get the validator to use.
        
        Prefers tool-specific validator, falls back to global default.
        
        Args:
            tool_config: Tool-specific config (may be None)
            
        Returns:
            Validator or None
        """
        if tool_config and tool_config.validator:
            return tool_config.validator
        return self._config.default_validator
    
    def _get_approval_handler(self, tool_config: Optional[ToolSecurityConfig]) -> Optional[ApprovalHandler]:
        """Get the approval handler to use.
        
        Prefers tool-specific handler, falls back to global default.
        
        Args:
            tool_config: Tool-specific config (may be None)
            
        Returns:
            ApprovalHandler or None
        """
        if tool_config and tool_config.approval_handler:
            return tool_config.approval_handler
        return self._config.default_approval_handler
    
    def _get_action_for_risk(
        self,
        risk_level: RiskLevel,
        tool_config: Optional[ToolSecurityConfig],
    ) -> DefaultAction:
        """Get the action for a risk level.
        
        Checks tool-specific policies first, then global defaults.
        
        Args:
            risk_level: The risk level to check
            tool_config: Tool-specific config (may be None)
            
        Returns:
            DefaultAction for the risk level
        """
        # Check tool-specific policies first
        if tool_config and risk_level in tool_config.risk_policies:
            return tool_config.risk_policies[risk_level]
        
        # Fall back to global policies
        if risk_level in self._config.default_risk_policies:
            return self._config.default_risk_policies[risk_level]
        
        # Ultimate fallback
        return self._config.default_action
    
    def add_tool_config(self, config: ToolSecurityConfig) -> "SecurityPolicy":
        """Add a tool configuration dynamically.
        
        Args:
            config: Tool security config to add
            
        Returns:
            Self for chaining
        """
        self._config.tool_configs.append(config)
        # Re-sort by priority
        self._config.tool_configs.sort(key=lambda c: c.priority, reverse=True)
        return self
    
    def remove_tool_config(self, pattern: str) -> "SecurityPolicy":
        """Remove tool configurations matching a pattern.
        
        Args:
            pattern: Pattern to match against tool_pattern
            
        Returns:
            Self for chaining
        """
        self._config.tool_configs = [
            c for c in self._config.tool_configs
            if not fnmatch.fnmatch(c.tool_pattern, pattern)
        ]
        return self


@dataclass
class SecurityCheckResult:
    """Result of a security policy check.
    
    Attributes:
        allowed: Whether execution is approved
        tool_name: Name of the tool checked
        arguments: Arguments that were checked
        validation_result: Result from validation phase (if any)
        reason: Human-readable explanation
    """
    allowed: bool
    tool_name: str
    arguments: Dict[str, Any]
    validation_result: Optional[ValidationResult]
    reason: str
    
    def __bool__(self) -> bool:
        """Allow using result in boolean context."""
        return self.allowed


# Convenience function for quick policy creation
def create_security_policy(
    dangerous_patterns: Optional[List[str]] = None,
    auto_approve_patterns: Optional[List[str]] = None,
    auto_deny_patterns: Optional[List[str]] = None,
    allowed_paths: Optional[List[str]] = None,
    require_approval: bool = True,
) -> SecurityPolicy:
    """Create a security policy with common defaults.
    
    This is a convenience function for quickly creating a security policy
    with typical configurations.
    
    Args:
        dangerous_patterns: Additional dangerous command patterns
        auto_approve_patterns: Patterns to auto-approve
        auto_deny_patterns: Patterns to auto-deny
        allowed_paths: Allowed file paths
        require_approval: Whether to require approval for suspicious operations
        
    Returns:
        Configured SecurityPolicy
    """
    # Build validator
    validators = []
    
    # Command validator
    cmd_validator = CommandValidator(
        dangerous_patterns=dangerous_patterns or []
    )
    validators.append(cmd_validator)
    
    # File path validator (if paths specified)
    if allowed_paths:
        path_validator = FilePathValidator(allowed_paths=allowed_paths)
        validators.append(path_validator)
    
    # Create composite validator if multiple
    if len(validators) == 1:
        validator = validators[0]
    else:
        from .validators import CompositeValidator
        validator = CompositeValidator(validators)
    
    # Build approval handler
    if require_approval:
        approval_handler = ConfigBasedApprovalHandler(
            auto_approve_patterns=auto_approve_patterns or [],
            auto_deny_patterns=auto_deny_patterns or [],
        )
    else:
        approval_handler = None
    
    config = SecurityPolicyConfig(
        default_validator=validator,
        default_approval_handler=approval_handler,
    )
    
    return SecurityPolicy(config)


# =============================================================================
# Legacy Classes (Backward Compatibility)
# =============================================================================

class CommandPolicy(Enum):
    """Legacy command policy enum."""
    ALLOW = "allow"
    DENY = "deny"
    ASK = "ask"


@dataclass
class SecurityConfig:
    """Legacy security configuration.
    
    .. deprecated::
        Use SecurityPolicyConfig and SecurityPolicy instead.
    """
    default_policy: CommandPolicy = CommandPolicy.ASK
    approval_handler: Optional[ApprovalHandler] = None
    approval_timeout_seconds: float = 30.0
    enable_logging: bool = True
    
    # Pattern lists for legacy compatibility
    allow_patterns: List[str] = field(default_factory=lambda: list(DEFAULT_ALLOW_PATTERNS))
    deny_patterns: List[str] = field(default_factory=lambda: list(DEFAULT_DENY_PATTERNS))
    ask_patterns: List[str] = field(default_factory=lambda: list(DEFAULT_ASK_PATTERNS))
    
    def add_allow_pattern(self, pattern: str) -> None:
        """Add an allow pattern."""
        self.allow_patterns.append(pattern)
    
    def add_deny_pattern(self, pattern: str) -> None:
        """Add a deny pattern."""
        self.deny_patterns.append(pattern)
    
    def add_ask_pattern(self, pattern: str) -> None:
        """Add an ask pattern."""
        self.ask_patterns.append(pattern)


@dataclass
class LegacySecurityCheckResult:
    """Legacy security check result.
    
    .. deprecated::
        Use SecurityCheckResult instead.
    """
    allowed: bool
    policy: CommandPolicy
    reason: str


class SecurityChecker:
    """Legacy security checker.
    
    .. deprecated::
        Use SecurityPolicy instead.
        
        Old:
            checker = SecurityChecker(config)
            result = await checker.check_and_approve(command)
            
        New:
            policy = SecurityPolicy(config)
            result = await policy.check(tool_name, arguments)
    """
    
    def __init__(self, config: Optional[SecurityConfig] = None):
        self._config = config or SecurityConfig()
        self._approval_handler = self._config.approval_handler
    
    def check_command(self, command: str) -> "LegacySecurityCheckResult":
        """Check command against security policy (synchronous version).
        
        This is the main method used by tests and legacy code.
        """
        import re
        
        # Check allow patterns first (highest priority)
        for pattern in self._config.allow_patterns:
            if re.search(pattern, command, re.IGNORECASE):
                return LegacySecurityCheckResult(
                    allowed=True,
                    policy=CommandPolicy.ALLOW,
                    reason=f"Matched allow pattern: {pattern}",
                )
        
        # Check deny patterns
        for pattern in self._config.deny_patterns:
            if re.search(pattern, command, re.IGNORECASE):
                return LegacySecurityCheckResult(
                    allowed=False,
                    policy=CommandPolicy.DENY,
                    reason=f"Matched deny pattern: {pattern}",
                )
        
        # Check ask patterns
        for pattern in self._config.ask_patterns:
            if re.search(pattern, command, re.IGNORECASE):
                return LegacySecurityCheckResult(
                    allowed=False,  # Not allowed yet, needs approval
                    policy=CommandPolicy.ASK,
                    reason=f"Matched ask pattern: {pattern}",
                )
        
        # No patterns matched, use default policy
        if self._config.default_policy == CommandPolicy.ALLOW:
            return LegacySecurityCheckResult(
                allowed=True,
                policy=CommandPolicy.ALLOW,
                reason="Default policy: allow",
            )
        elif self._config.default_policy == CommandPolicy.DENY:
            return LegacySecurityCheckResult(
                allowed=False,
                policy=CommandPolicy.DENY,
                reason="Default policy: deny",
            )
        else:  # ASK
            return LegacySecurityCheckResult(
                allowed=False,
                policy=CommandPolicy.ASK,
                reason="Default policy: ask",
            )
    
    async def check(self, command: str) -> "LegacySecurityCheckResult":
        """Check command against security policy (async version)."""
        import asyncio
        from .exceptions import CommandRejectedError, ApprovalTimeoutError
        
        result = self.check_command(command)
        
        # Handle DENY policy - raise exception
        if result.policy == CommandPolicy.DENY:
            raise CommandRejectedError(
                f"Command rejected: {command}. Reason: {result.reason}"
            )
        
        # Handle ASK policy
        if result.policy == CommandPolicy.ASK:
            if self._approval_handler:
                try:
                    # Apply timeout if configured
                    timeout = self._config.approval_timeout_seconds
                    if timeout > 0:
                        approved = await asyncio.wait_for(
                            self._approval_handler.approve(
                                command=command,
                                reason=result.reason,
                            ),
                            timeout=timeout,
                        )
                    else:
                        approved = await self._approval_handler.approve(
                            command=command,
                            reason=result.reason,
                        )
                    
                    if not approved:
                        raise CommandRejectedError(
                            f"Command rejected by approval handler: {command}"
                        )
                    
                    return LegacySecurityCheckResult(
                        allowed=True,
                        policy=CommandPolicy.ASK,
                        reason="Approved by handler",
                    )
                except asyncio.TimeoutError:
                    # Handle timeout based on default policy
                    if self._config.default_policy == CommandPolicy.ALLOW:
                        return LegacySecurityCheckResult(
                            allowed=True,
                            policy=CommandPolicy.ASK,
                            reason=f"Approval timeout ({timeout}s), defaulting to allow",
                        )
                    else:
                        raise ApprovalTimeoutError(
                            f"Approval timed out ({timeout}s), defaulting to deny"
                        )
            else:
                # No handler configured
                if self._config.default_policy == CommandPolicy.ALLOW:
                    return LegacySecurityCheckResult(
                        allowed=True,
                        policy=CommandPolicy.ASK,
                        reason="No handler configured, defaulting to allow",
                    )
                else:
                    raise CommandRejectedError(
                        f"Command requires approval but no handler configured: {command}"
                    )
        
        return result
    
    async def check_and_approve(self, command: str) -> "LegacySecurityCheckResult":
        """Legacy method alias for check()."""
        return await self.check(command)


# Legacy pattern constants
DEFAULT_ALLOW_PATTERNS: List[str] = [
    r"^git\s+status",
    r"^git\s+log",
    r"^ls\s+",
    r"^pwd$",
    r"^echo\s+",
    r"^cat\s+",
    r"^head\s+",
    r"^tail\s+",
    r"^grep\s+",
    r"^find\s+",
]

DEFAULT_DENY_PATTERNS: List[str] = [
    r"rm\s+-rf\s+/$",  # Match rm -rf / exactly or with trailing space
    r"rm\s+-rf\s+/\s*$",  # Match rm -rf / with optional trailing space
    r":\(\)\s*\{\s*:\|:\s*&\s*\};\s*:",  # Fork bomb
    r"shutdown\s+-h\s+now",
    r"mkfs\.",
    r"dd\s+if=\S+\s+of=/dev/\w+",
    r"format\s+C:",  # Windows format command
]

DEFAULT_ASK_PATTERNS: List[str] = [
    r"sudo\s+",
    r"su\s+-",
    r"rm\s+-rf\s+",
    r"curl\s+.*\|\s*sh",
    r"wget\s+.*\|\s*sh",
]
