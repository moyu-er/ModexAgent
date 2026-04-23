"""
示例6: 命令执行安全控制与审批处理器

演示如何使用三级安全模型 (ALLOW/ASK/DENY) 和多种审批处理器
控制命令执行，包括：
- ConsoleApprovalHandler: 命令行交互审批
- ConfigBasedApprovalHandler: 配置自动审批
- CompositeApprovalHandler: 组合审批
- 自定义审批处理器
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from framework.sandbox import (
    get_default_sandbox,
    SandboxConfig,
)
from framework.security import (
    SecurityConfig,
    CommandPolicy,
    ConsoleApprovalHandler,
    ConfigBasedApprovalHandler,
    CompositeApprovalHandler,
    LoggingApprovalHandler,
    ApprovalHandler,
)


async def demo_console_handler():
    """【场景1】命令行交互审批 - 适合开发调试"""
    print("\n" + "=" * 60)
    print("【场景1】命令行交互审批 (ConsoleApprovalHandler)")
    print("=" * 60)
    
    config = SandboxConfig(
        workspace_dir="/tmp/sandbox_demo",
        max_execution_time_seconds=10,
        security=SecurityConfig(
            default_policy=CommandPolicy.ASK,
            approval_handler=ConsoleApprovalHandler(),
        ),
    )
    
    sandbox = get_default_sandbox()
    
    # 白名单命令 - 直接执行
    print("\n1. 白名单命令 (echo) - 直接执行:")
    result = await sandbox.execute_command("echo 'Hello World'", config=config)
    print(f"   结果: {'✓' if result.success else '✗'} {result.stdout.strip()}")
    
    # 需要审批的命令
    print("\n2. 需要审批的命令 (ls -la /tmp):")
    result = await sandbox.execute_command("ls -la /tmp", config=config)
    print(f"   结果: {'✓' if result.success else '✗'}")
    
    # 黑名单命令 - 被拒绝
    print("\n3. 黑名单命令 (rm -rf /) - 直接拒绝:")
    result = await sandbox.execute_command("rm -rf /", config=config)
    print(f"   结果: {'✓' if result.success else '✗'} {result.error}")


async def demo_config_handler():
    """【场景2】配置自动审批 - 适合自动化/CI环境"""
    print("\n" + "=" * 60)
    print("【场景2】配置自动审批 (ConfigBasedApprovalHandler)")
    print("=" * 60)
    print("特点: 无需人工交互，根据配置自动决策")
    
    config = SandboxConfig(
        workspace_dir="/tmp/sandbox_demo",
        max_execution_time_seconds=10,
        security=SecurityConfig(
            default_policy=CommandPolicy.ASK,
            approval_handler=ConfigBasedApprovalHandler(
                # 自动批准的命令
                auto_approve_patterns=[
                    r"^git\s+status",
                    r"^git\s+log",
                    r"^ls\s+",
                    r"^echo\s+",
                ],
                # 自动拒绝的命令
                auto_deny_patterns=[
                    r"^rm\s+-rf\s+/",
                    r"^format\s+",
                ],
                # 默认策略: 不匹配时拒绝
                default_action=False,
            ),
        ),
    )
    
    sandbox = get_default_sandbox()
    
    test_commands = [
        ("git status", "自动批准"),
        ("ls -la /tmp", "自动批准"),
        ("rm -rf /", "自动拒绝"),
        ("sudo apt update", "默认拒绝"),
    ]
    
    for cmd, expected in test_commands:
        print(f"\n  命令: {cmd}")
        print(f"  预期: {expected}")
        result = await sandbox.execute_command(cmd, config=config)
        print(f"  结果: {'✓ 执行' if result.success else '✗ 拒绝'}")


async def demo_composite_handler():
    """【场景3】组合审批 - 日志记录 + 配置检查 + 人工确认"""
    print("\n" + "=" * 60)
    print("【场景3】组合审批 (CompositeApprovalHandler)")
    print("=" * 60)
    print("特点: 链式处理，先记录日志，再检查配置，最后人工确认")
    
    config = SandboxConfig(
        workspace_dir="/tmp/sandbox_demo",
        max_execution_time_seconds=10,
        security=SecurityConfig(
            default_policy=CommandPolicy.ASK,
            approval_handler=CompositeApprovalHandler([
                # 1. 先记录所有审批请求
                LoggingApprovalHandler(),
                # 2. 检查配置规则
                ConfigBasedApprovalHandler(
                    auto_approve_patterns=[r"^git\s+status"],
                    auto_deny_patterns=[r"^rm\s+-rf\s+/"],
                    default_action=True,  # 配置未匹配时继续下一个handler
                ),
                # 3. 最后人工确认
                ConsoleApprovalHandler(),
            ]),
        ),
    )
    
    sandbox = get_default_sandbox()
    
    print("\n  测试组合审批链:")
    print("  - git status: 配置自动批准")
    result = await sandbox.execute_command("git status", config=config)
    print(f"    结果: {'✓' if result.success else '✗'}")
    
    print("\n  - rm -rf /: 配置自动拒绝")
    result = await sandbox.execute_command("rm -rf /", config=config)
    print(f"    结果: {'✓' if result.success else '✗'}")


async def demo_custom_handler():
    """【场景4】自定义审批处理器 - 企业级场景"""
    print("\n" + "=" * 60)
    print("【场景4】自定义审批处理器示例")
    print("=" * 60)
    print("演示如何实现自定义审批逻辑（如发送到Slack、邮件等）")
    
    class SlackApprovalHandler(ApprovalHandler):
        """示例: Slack 审批处理器"""
        
        def __init__(self, webhook_url: str = None):
            self.webhook_url = webhook_url
            self.pending_approvals = {}
        
        async def approve(self, command: str, reason: str) -> bool:
            # 实际实现中，这里会:
            # 1. 发送 Slack 消息到审批频道
            # 2. 等待管理员点击"批准"或"拒绝"按钮
            # 3. 返回审批结果
            
            print(f"\n    [Slack 审批模拟]")
            print(f"    发送到 Slack: 需要审批命令 `{command}`")
            print(f"    原因: {reason}")
            # 模拟: 假设管理员批准了
            return True
        
        @property
        def name(self) -> str:
            return "slack"
    
    class EmailApprovalHandler(ApprovalHandler):
        """示例: 邮件审批处理器"""
        
        async def approve(self, command: str, reason: str) -> bool:
            print(f"\n    [邮件审批模拟]")
            print(f"    发送邮件给管理员: 需要审批命令 `{command}`")
            # 模拟: 假设邮件审批通过
            return True
        
        @property
        def name(self) -> str:
            return "email"
    
    # 使用自定义 handler
    config = SandboxConfig(
        workspace_dir="/tmp/sandbox_demo",
        security=SecurityConfig(
            default_policy=CommandPolicy.ASK,
            approval_handler=SlackApprovalHandler(),
        ),
    )
    
    sandbox = get_default_sandbox()
    
    print("\n  测试 Slack 审批:")
    result = await sandbox.execute_command("sudo apt update", config=config)
    print(f"  结果: {'✓ 执行' if result.success else '✗ 拒绝'}")


async def demo_security_patterns():
    """【场景5】安全策略配置示例"""
    print("\n" + "=" * 60)
    print("【场景5】安全策略配置示例")
    print("=" * 60)
    
    # 严格模式: 默认拒绝，只有明确允许的命令才能执行
    strict_config = SandboxConfig(
        workspace_dir="/tmp/sandbox_demo",
        security=SecurityConfig(
            default_policy=CommandPolicy.DENY,  # 默认拒绝
            allow_patterns=[
                r"^echo\s+",
                r"^ls\s+",
                r"^pwd\s*$",
            ],
            # 危险命令
            deny_patterns=[
                r"rm\s+-rf\s+/$",
                r"format\s+",
                r":\(\)\s*\{",  # Fork bomb
            ],
        ),
    )
    
    # 宽松模式: 默认允许，只有明确禁止的命令才会被拒绝
    permissive_config = SandboxConfig(
        workspace_dir="/tmp/sandbox_demo",
        security=SecurityConfig(
            default_policy=CommandPolicy.ALLOW,  # 默认允许
            deny_patterns=[
                r"rm\s+-rf\s+/$",
                r"format\s+",
                r"mkfs\.\w+",
            ],
        ),
    )
    
    sandbox = get_default_sandbox()
    
    print("\n  严格模式测试 (默认拒绝):")
    test_commands = ["echo hello", "whoami", "rm -rf /"]
    for cmd in test_commands:
        result = await sandbox.execute_command(cmd, config=strict_config)
        status = "✓ 允许" if result.success else "✗ 拒绝"
        print(f"    {cmd}: {status}")
    
    print("\n  宽松模式测试 (默认允许):")
    for cmd in test_commands:
        result = await sandbox.execute_command(cmd, config=permissive_config)
        status = "✓ 允许" if result.success else "✗ 拒绝"
        print(f"    {cmd}: {status}")


async def main():
    """主函数 - 运行所有示例"""
    print("\n" + "=" * 60)
    print("命令执行安全控制与审批处理器演示")
    print("=" * 60)
    print("\n本示例演示:")
    print("  1. 三级安全模型: ALLOW(白名单) / ASK(需审批) / DENY(黑名单)")
    print("  2. 多种审批处理器: 命令行、配置驱动、组合式")
    print("  3. 自定义审批处理器: 可扩展实现 Slack、邮件等")
    print("  4. 灵活的安全策略配置")
    
    # 运行各个示例
    await demo_console_handler()
    await demo_config_handler()
    await demo_composite_handler()
    await demo_custom_handler()
    await demo_security_patterns()
    
    print("\n" + "=" * 60)
    print("演示完成!")
    print("=" * 60)
    print("\n提示:")
    print("  - ConsoleApprovalHandler: 适合交互式开发环境")
    print("  - ConfigBasedApprovalHandler: 适合自动化/CI环境")
    print("  - CompositeApprovalHandler: 适合复杂审批流程")
    print("  - 自定义 Handler: 可集成企业审批系统")


if __name__ == "__main__":
    asyncio.run(main())
