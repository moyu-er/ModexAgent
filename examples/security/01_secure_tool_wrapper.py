"""Example: Secure Tool Wrapper with Validation and Approval.

This example demonstrates how to use SecureToolWrapper to add
security validation and approval to existing tools.

The two-phase security model:
1. Validation: Check tool arguments for dangerous patterns
2. Approval: Get approval for suspicious operations
"""

import asyncio
from framework.security import (
    CommandValidator,
    FilePathValidator,
    CompositeValidator,
    ConsoleApprovalHandler,
    ConfigBasedApprovalHandler,
    RiskLevel,
    DefaultAction,
)
from framework.tools.secure_wrapper import SecureToolConfig
from framework.tools.types import ToolResult


# Create a simple mock tool interface for demonstration
class MockTool:
    """Mock tool for demonstration."""
    
    def __init__(self, name: str):
        self._name = name
    
    @property
    def name(self) -> str:
        return self._name
    
    async def execute(self, **kwargs) -> ToolResult:
        """Execute the tool (mock)."""
        print(f"  [Mock] Executing {self._name} with args: {kwargs}")
        return ToolResult(
            tool_name=self._name,
            success=True,
            result={"output": f"Executed {self._name}"},
            error=None
        )


async def example_1_basic_validation():
    """Example 1: Basic command validation."""
    print("=" * 60)
    print("Example 1: Basic Command Validation")
    print("=" * 60)
    
    # Create a simple command validator
    validator = CommandValidator()
    
    # Test various commands
    test_commands = [
        "ls -la",                      # Safe
        "cat /etc/passwd",             # Safe
        "rm -rf /tmp/test",            # Suspicious (rm -rf)
        "rm -rf /",                    # Dangerous (blocked)
        "sudo apt update",             # Dangerous (sudo)
    ]
    
    for cmd in test_commands:
        result = await validator.validate("bash", {"command": cmd})
        print(f"Command: {cmd}")
        print(f"  Status: {result.status.value}")
        print(f"  Risk: {result.risk_level.name}")
        print(f"  Reason: {result.reason}")
        print()


async def example_2_file_path_validation():
    """Example 2: File path validation."""
    print("=" * 60)
    print("Example 2: File Path Validation")
    print("=" * 60)
    
    # Create a file path validator with allowed paths
    validator = FilePathValidator(
        allowed_paths=["/tmp", "/home/user"],
        block_traversal=True
    )
    
    # Test various paths
    test_paths = [
        "/tmp/test.txt",               # Allowed
        "/home/user/documents/file",   # Allowed
        "../../../etc/passwd",         # Suspicious (traversal)
        "/etc/passwd",                 # Suspicious (outside allowed)
    ]
    
    for path in test_paths:
        result = await validator.validate("read_file", {"path": path})
        print(f"Path: {path}")
        print(f"  Status: {result.status.value}")
        print(f"  Risk: {result.risk_level.name}")
        print()


async def example_3_composite_validation():
    """Example 3: Composite validation (command + file path)."""
    print("=" * 60)
    print("Example 3: Composite Validation")
    print("=" * 60)
    
    # Create a composite validator
    validator = CompositeValidator([
        CommandValidator(),
        FilePathValidator(allowed_paths=["/tmp"]),
    ])
    
    # Test with both command and path
    test_cases = [
        {"command": "ls /tmp", "path": "/tmp/test.txt"},  # Safe
        {"command": "rm -rf /", "path": "/tmp/test.txt"}, # Dangerous
        {"command": "ls /tmp", "path": "/etc/passwd"},   # Suspicious path
    ]
    
    for args in test_cases:
        result = await validator.validate("tool", args)
        print(f"Args: {args}")
        print(f"  Status: {result.status.value}")
        print(f"  Risk: {result.risk_level.name}")
        print()


async def example_4_risk_policies():
    """Example 4: Risk-based policies."""
    print("=" * 60)
    print("Example 4: Risk-Based Policies")
    print("=" * 60)
    
    # Create secure tool configuration with different policies per risk level
    config = SecureToolConfig(
        validator=CommandValidator(),
        approval_handler=None,  # No handler for this demo
        risk_policies={
            RiskLevel.LOW: DefaultAction.ALLOW,
            RiskLevel.MEDIUM: DefaultAction.ASK,
            RiskLevel.HIGH: DefaultAction.ASK,
            RiskLevel.CRITICAL: DefaultAction.DENY,
        }
    )
    
    print("Risk Policies:")
    for risk, action in config.risk_policies.items():
        print(f"  {risk.name} -> {action.value}")
    print()
    
    # Test how different commands map to different actions
    test_commands = [
        ("ls -la", "Safe command"),
        ("rm -rf /tmp/test", "Suspicious command"),
        ("rm -rf /", "Dangerous command"),
    ]
    
    for cmd, desc in test_commands:
        result = await config.validator.validate("bash", {"command": cmd})
        action = config.risk_policies.get(result.risk_level, config.default_action)
        print(f"Command: {cmd}")
        print(f"  Description: {desc}")
        print(f"  Risk Level: {result.risk_level.name}")
        print(f"  Action: {action.value}")
        print()


async def example_5_config_based_approval():
    """Example 5: Config-based approval handler."""
    print("=" * 60)
    print("Example 5: Config-Based Approval Handler")
    print("=" * 60)
    
    # Create config-based approval handler
    approval_handler = ConfigBasedApprovalHandler(
        auto_approve_patterns=[r"^ls\s+"],  # Auto-approve ls commands
        auto_deny_patterns=[r"rm\s+-rf\s+/"],  # Auto-reject dangerous
    )
    
    # Test commands
    test_commands = [
        "ls -la /tmp",
        "cat /etc/passwd",
        "rm -rf /",
    ]
    
    for cmd in test_commands:
        approved = await approval_handler.approve(cmd, "Test approval")
        status = "APPROVED" if approved else "DENIED"
        print(f"Command: {cmd}")
        print(f"  Result: {status}")
        print()


async def main():
    """Run all examples."""
    print("\n" + "=" * 60)
    print("Secure Tool Wrapper Examples")
    print("=" * 60 + "\n")
    
    await example_1_basic_validation()
    await example_2_file_path_validation()
    await example_3_composite_validation()
    await example_4_risk_policies()
    await example_5_config_based_approval()
    
    print("\n" + "=" * 60)
    print("Examples completed!")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
