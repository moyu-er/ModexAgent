"""Example: Unified Security Policy with Per-Tool Configuration.

This example demonstrates the new unified SecurityPolicy system that combines
validation and approval into a single configurable system with support for
per-tool configuration and pattern matching.
"""

import asyncio
from framework.security import (
    SecurityPolicy,
    SecurityPolicyConfig,
    ToolSecurityConfig,
    CommandValidator,
    FilePathValidator,
    ConfigBasedApprovalHandler,
    RiskLevel,
    DefaultAction,
)


async def example_1_basic_policy():
    """Example 1: Basic security policy."""
    print("=" * 60)
    print("Example 1: Basic Security Policy")
    print("=" * 60)
    
    # Create a basic policy with default validator
    policy = SecurityPolicy(SecurityPolicyConfig(
        default_validator=CommandValidator(),
    ))
    
    # Test various commands
    test_cases = [
        ("bash", {"command": "ls -la"}),
        ("bash", {"command": "rm -rf /"}),
        ("python", {"code": "print('hello')"}),
    ]
    
    for tool_name, args in test_cases:
        result = await policy.check(tool_name, args)
        print(f"Tool: {tool_name}, Args: {args}")
        print(f"  Allowed: {result.allowed}")
        print(f"  Reason: {result.reason}")
        print()


async def example_2_per_tool_config():
    """Example 2: Per-tool configuration with pattern matching."""
    print("=" * 60)
    print("Example 2: Per-Tool Configuration")
    print("=" * 60)
    
    # Create policy with per-tool configurations
    policy = SecurityPolicy(SecurityPolicyConfig(
        tool_configs=[
            # Exact match for 'bash' tool with command validator
            ToolSecurityConfig(
                tool_pattern="bash",
                validator=CommandValidator(),
                priority=10,
            ),
            # Wildcard match for all file_* tools
            ToolSecurityConfig(
                tool_pattern="file_*",
                validator=FilePathValidator(allowed_paths=["/tmp", "/home"]),
                priority=5,
            ),
            # Universal wildcard as fallback
            ToolSecurityConfig(
                tool_pattern="*",
                validator=None,  # No validation for other tools
                priority=0,
            ),
        ]
    ))
    
    # Test different tools
    test_cases = [
        ("bash", {"command": "rm -rf /"}),           # Should be validated
        ("file_read", {"path": "/tmp/test.txt"}),   # Should be validated
        ("file_write", {"path": "/etc/passwd"}),    # Should be validated
        ("other_tool", {"data": "anything"}),       # Should pass (no validator)
    ]
    
    for tool_name, args in test_cases:
        result = await policy.check(tool_name, args)
        print(f"Tool: {tool_name}")
        print(f"  Allowed: {result.allowed}")
        print(f"  Reason: {result.reason}")
        print()


async def example_3_risk_based_policies():
    """Example 3: Risk-based policies per tool."""
    print("=" * 60)
    print("Example 3: Risk-Based Policies")
    print("=" * 60)
    
    # Create policy with different risk policies for different tools
    policy = SecurityPolicy(SecurityPolicyConfig(
        tool_configs=[
            ToolSecurityConfig(
                tool_pattern="bash",
                validator=CommandValidator(),
                risk_policies={
                    RiskLevel.CRITICAL: DefaultAction.DENY,  # Auto-deny critical
                    RiskLevel.HIGH: DefaultAction.ASK,       # Ask for high risk
                    RiskLevel.MEDIUM: DefaultAction.ASK,
                    RiskLevel.LOW: DefaultAction.ALLOW,
                },
                priority=10,
            ),
        ],
        default_risk_policies={
            RiskLevel.CRITICAL: DefaultAction.DENY,
            RiskLevel.HIGH: DefaultAction.DENY,
            RiskLevel.MEDIUM: DefaultAction.ASK,
            RiskLevel.LOW: DefaultAction.ALLOW,
        }
    ))
    
    # Test commands with different risk levels
    test_cases = [
        ("bash", {"command": "ls -la"}, "LOW"),
        ("bash", {"command": "rm -rf /tmp"}, "HIGH/ASK"),
        ("bash", {"command": "rm -rf /"}, "CRITICAL/DENY"),
    ]
    
    for tool_name, args, expected in test_cases:
        result = await policy.check(tool_name, args)
        print(f"Tool: {tool_name}, Expected: {expected}")
        print(f"  Allowed: {result.allowed}")
        print(f"  Reason: {result.reason}")
        print()


async def example_4_approval_handler():
    """Example 4: Policy with approval handler."""
    print("=" * 60)
    print("Example 4: Policy with Approval Handler")
    print("=" * 60)
    
    # Create policy with auto-approve patterns
    policy = SecurityPolicy(SecurityPolicyConfig(
        tool_configs=[
            ToolSecurityConfig(
                tool_pattern="bash",
                validator=CommandValidator(),
                approval_handler=ConfigBasedApprovalHandler(
                    auto_approve_patterns=[r"^ls\s+"],
                    auto_deny_patterns=[r"rm\s+-rf\s+/"],
                ),
                risk_policies={
                    RiskLevel.HIGH: DefaultAction.ASK,
                },
                priority=10,
            ),
        ]
    ))
    
    # Test commands
    test_cases = [
        ("bash", {"command": "ls -la /tmp"}, "Auto-approved"),
        ("bash", {"command": "rm -rf /tmp"}, "Needs approval"),
        ("bash", {"command": "rm -rf /"}, "Blocked by validator"),
    ]
    
    for tool_name, args, desc in test_cases:
        result = await policy.check(tool_name, args)
        print(f"Command: {args['command']}")
        print(f"  Description: {desc}")
        print(f"  Allowed: {result.allowed}")
        print(f"  Reason: {result.reason}")
        print()


async def example_5_dynamic_config():
    """Example 5: Dynamic configuration updates."""
    print("=" * 60)
    print("Example 5: Dynamic Configuration")
    print("=" * 60)
    
    # Create initial policy
    policy = SecurityPolicy(SecurityPolicyConfig())
    
    # Initially, no validation
    result = await policy.check("bash", {"command": "rm -rf /"})
    print(f"Before adding config: allowed={result.allowed}")
    
    # Dynamically add tool configuration
    policy.add_tool_config(ToolSecurityConfig(
        tool_pattern="bash",
        validator=CommandValidator(),
        priority=10,
    ))
    
    # Now validation is applied
    result = await policy.check("bash", {"command": "rm -rf /"})
    print(f"After adding config: allowed={result.allowed}, reason={result.reason}")
    print()


async def example_6_wildcard_patterns():
    """Example 6: Advanced wildcard patterns."""
    print("=" * 60)
    print("Example 6: Advanced Wildcard Patterns")
    print("=" * 60)
    
    # Create policy with various wildcard patterns
    policy = SecurityPolicy(SecurityPolicyConfig(
        tool_configs=[
            # Match any tool starting with 'db_'
            ToolSecurityConfig(
                tool_pattern="db_*",
                validator=CommandValidator(),
                priority=10,
            ),
            # Match any tool ending with '_write'
            ToolSecurityConfig(
                tool_pattern="*_write",
                validator=FilePathValidator(allowed_paths=["/tmp"]),
                priority=8,
            ),
            # Match any tool with 'admin' in the name
            ToolSecurityConfig(
                tool_pattern="*admin*",
                validator=CommandValidator(),
                priority=5,
            ),
        ]
    ))
    
    # Test different tool names
    test_tools = [
        "db_query",
        "db_migrate",
        "file_write",
        "config_write",
        "admin_panel",
        "superadmin_tool",
        "regular_tool",
    ]
    
    for tool_name in test_tools:
        result = await policy.check(tool_name, {"command": "test"})
        print(f"Tool: {tool_name:20} -> Allowed: {result.allowed}")
    print()


async def main():
    """Run all examples."""
    print("\n" + "=" * 60)
    print("Unified Security Policy Examples")
    print("=" * 60 + "\n")
    
    await example_1_basic_policy()
    await example_2_per_tool_config()
    await example_3_risk_based_policies()
    await example_4_approval_handler()
    await example_5_dynamic_config()
    await example_6_wildcard_patterns()
    
    print("\n" + "=" * 60)
    print("Examples completed!")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
