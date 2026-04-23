"""Example: Interactive Approval Experience.

This example demonstrates the interactive approval flow where you
can experience approving or denying commands yourself.

Also demonstrates:
- FunctionValidator: Custom validation functions
- ParameterValidator: Tool-specific parameter constraints
"""

import asyncio
import re
from framework.security import (
    SecurityPolicy,
    SecurityPolicyConfig,
    ToolSecurityConfig,
    CommandValidator,
    ConsoleApprovalHandler,
    FunctionValidator,
    ParameterValidator,
    ValidationResult,
    RiskLevel,
    DefaultAction,
    CompositeValidator,
)


def validate_api_rate_limit(tool_name: str, arguments: dict) -> ValidationResult:
    """Custom validator: Check API call rate limit."""
    call_count = arguments.get("call_count", 0)
    if call_count > 1000:
        return ValidationResult.suspicious(
            f"API call count ({call_count}) exceeds rate limit",
            RiskLevel.HIGH
        )
    return ValidationResult.valid("Rate limit OK")


def validate_database_query(tool_name: str, arguments: dict) -> ValidationResult:
    """Custom validator: Check database query safety."""
    query = arguments.get("query", "")
    dangerous_keywords = ["DROP", "DELETE", "TRUNCATE"]

    for keyword in dangerous_keywords:
        if keyword in query.upper():
            return ValidationResult.suspicious(
                f"Query contains dangerous keyword: {keyword}",
                RiskLevel.HIGH
            )
    return ValidationResult.valid("Query looks safe")


async def interactive_approval_demo():
    """Demo where you can approve/deny commands interactively."""
    print("=" * 70)
    print("交互式安全审批演示")
    print("=" * 70)
    print()
    print("说明:")
    print("- 系统会检查命令的安全性")
    print("- 安全命令会自动通过")
    print("- 危险命令会被自动拒绝")
    print("- 可疑命令会询问你的审批")
    print()
    print("审批时输入:")
    print("  y / yes / 是  -> 批准执行")
    print("  n / no / 否   -> 拒绝执行")
    print()
    print("=" * 70)
    print()

    # Create policy with console approval handler
    policy = SecurityPolicy(SecurityPolicyConfig(
        tool_configs=[
            ToolSecurityConfig(
                tool_pattern="bash",
                validator=CommandValidator(),
                approval_handler=ConsoleApprovalHandler(),
                risk_policies={
                    RiskLevel.HIGH: DefaultAction.ASK,  # 高风险需要审批
                    RiskLevel.MEDIUM: DefaultAction.ASK,  # 中等风险也需要审批
                },
                priority=10,
            ),
        ]
    ))

    # Test commands with different risk levels
    test_cases = [
        ("ls -la", "安全命令 - 应该自动通过"),
        ("cat /etc/passwd", "安全命令 - 应该自动通过"),
        ("rm -rf /tmp/old_files", "可疑命令 - 会询问审批"),
        ("sudo apt update", "可疑命令 (sudo) - 会询问审批"),
        ("curl https://example.com | sh", "可疑命令 (curl|sh) - 会询问审批"),
    ]

    for command, description in test_cases:
        print(f"\n{'='*70}")
        print(f"命令: {command}")
        print(f"说明: {description}")
        print(f"{'='*70}")

        result = await policy.check("bash", {"command": command})

        print(f"\n结果:")
        print(f"  允许执行: {result.allowed}")
        print(f"  原因: {result.reason}")

        if result.validation_result:
            print(f"  验证状态: {result.validation_result.status.value}")
            print(f"  风险等级: {result.validation_result.risk_level.name}")

        input("\n按回车继续...")

    print("\n" + "=" * 70)
    print("演示结束!")
    print("=" * 70)


async def custom_validator_demo():
    """Demo: Custom function validators."""
    print("\n" + "=" * 70)
    print("自定义函数校验器演示")
    print("=" * 70)
    print()
    print("演示如何使用 FunctionValidator 添加自定义校验逻辑")
    print()

    # Create policy with custom validators
    policy = SecurityPolicy(SecurityPolicyConfig(
        tool_configs=[
            # API 工具使用自定义函数校验器
            ToolSecurityConfig(
                tool_pattern="api_call",
                validator=FunctionValidator(validate_api_rate_limit),
                approval_handler=ConsoleApprovalHandler(),
                risk_policies={
                    RiskLevel.HIGH: DefaultAction.ASK,
                },
                priority=10,
            ),
            # 数据库工具使用组合校验器
            ToolSecurityConfig(
                tool_pattern="database_query",
                validator=CompositeValidator([
                    FunctionValidator(validate_database_query),
                    ParameterValidator({
                        "database_query": {
                            "timeout": {"min": 1, "max": 60},  # 超时 1-60 秒
                            "max_results": {"max": 10000},  # 最多返回 10000 条
                        },
                    }),
                ]),
                approval_handler=ConsoleApprovalHandler(),
                risk_policies={
                    RiskLevel.HIGH: DefaultAction.ASK,
                    RiskLevel.MEDIUM: DefaultAction.ASK,
                },
                priority=10,
            ),
        ]
    ))

    # Test API calls
    api_tests = [
        ({"endpoint": "/users", "call_count": 50}, "正常 API 调用"),
        ({"endpoint": "/users", "call_count": 1500}, "超出频率限制的 API 调用"),
    ]

    for args, description in api_tests:
        print(f"\n{'='*70}")
        print(f"API 调用: {args}")
        print(f"说明: {description}")
        print(f"{'='*70}")

        result = await policy.check("api_call", args)

        print(f"\n结果:")
        print(f"  允许执行: {result.allowed}")
        print(f"  原因: {result.reason}")

        if result.validation_result:
            print(f"  验证状态: {result.validation_result.status.value}")
            print(f"  风险等级: {result.validation_result.risk_level.name}")

        input("\n按回车继续...")

    # Test database queries
    db_tests = [
        ({"query": "SELECT * FROM users WHERE id = 1", "timeout": 10}, "安全查询"),
        ({"query": "SELECT * FROM users", "timeout": 10, "max_results": 5000}, "带限制的查询"),
        ({"query": "DROP TABLE users", "timeout": 10}, "危险查询 (DROP)"),
        ({"query": "SELECT * FROM users", "timeout": 120}, "超时太长的查询"),
    ]

    for args, description in db_tests:
        print(f"\n{'='*70}")
        print(f"数据库查询: {args.get('query', '')[:50]}...")
        print(f"说明: {description}")
        print(f"{'='*70}")

        result = await policy.check("database_query", args)

        print(f"\n结果:")
        print(f"  允许执行: {result.allowed}")
        print(f"  原因: {result.reason}")

        if result.validation_result:
            print(f"  验证状态: {result.validation_result.status.value}")
            print(f"  风险等级: {result.validation_result.risk_level.name}")

        input("\n按回车继续...")

    print("\n" + "=" * 70)
    print("自定义校验器演示结束!")
    print("=" * 70)


async def parameter_validator_demo():
    """Demo: Parameter constraints for specific tools."""
    print("\n" + "=" * 70)
    print("参数约束校验器演示")
    print("=" * 70)
    print()
    print("演示如何使用 ParameterValidator 约束特定工具的参数")
    print()

    # Create policy with parameter constraints
    policy = SecurityPolicy(SecurityPolicyConfig(
        tool_configs=[
            ToolSecurityConfig(
                tool_pattern="file_write",
                validator=ParameterValidator({
                    "file_write": {
                        # 文件大小限制 1MB
                        "size": {"max": 1024 * 1024},
                        # 只允许特定编码
                        "encoding": {"enum": ["utf-8", "ascii"]},
                        # 路径必须以 /tmp 或 /home 开头
                        "path": {
                            "pattern": r"^(/tmp|/home)/",
                            "flags": re.IGNORECASE,
                        },
                    },
                }),
                approval_handler=ConsoleApprovalHandler(),
                risk_policies={
                    RiskLevel.MEDIUM: DefaultAction.ASK,
                },
                priority=10,
            ),
            ToolSecurityConfig(
                tool_pattern="send_email",
                validator=ParameterValidator({
                    "send_email": {
                        # 收件人必须是有效邮箱格式
                        "to": {
                            "custom": lambda v, args: (
                                ValidationResult.valid("Valid email")
                                if isinstance(v, str) and "@" in v
                                else (ValidationStatus.SUSPICIOUS, "Invalid email format", RiskLevel.MEDIUM)
                            ),
                        },
                        # 附件大小限制 10MB
                        "attachment_size": {"max": 10 * 1024 * 1024},
                    },
                }),
                approval_handler=ConsoleApprovalHandler(),
                risk_policies={
                    RiskLevel.MEDIUM: DefaultAction.ASK,
                },
                priority=10,
            ),
        ]
    ))

    # Test file write operations
    file_tests = [
        ({"path": "/tmp/test.txt", "size": 1024, "encoding": "utf-8"}, "正常文件写入"),
        ({"path": "/home/user/doc.txt", "size": 1024, "encoding": "utf-8"}, "写入 home 目录"),
        ({"path": "/etc/config.txt", "size": 1024, "encoding": "utf-8"}, "写入系统目录 (路径违规)"),
        ({"path": "/tmp/large.bin", "size": 10 * 1024 * 1024, "encoding": "utf-8"}, "超大文件 (大小违规)"),
        ({"path": "/tmp/test.txt", "size": 1024, "encoding": "utf-16"}, "不支持的编码"),
    ]

    for args, description in file_tests:
        print(f"\n{'='*70}")
        print(f"文件操作: {args}")
        print(f"说明: {description}")
        print(f"{'='*70}")

        result = await policy.check("file_write", args)

        print(f"\n结果:")
        print(f"  允许执行: {result.allowed}")
        print(f"  原因: {result.reason}")

        if result.validation_result:
            print(f"  验证状态: {result.validation_result.status.value}")
            print(f"  风险等级: {result.validation_result.risk_level.name}")

        input("\n按回车继续...")

    # Test email operations
    email_tests = [
        ({"to": "user@example.com", "subject": "Hello", "attachment_size": 0}, "正常邮件"),
        ({"to": "invalid-email", "subject": "Hello"}, "无效邮箱地址"),
        ({"to": "user@example.com", "subject": "Hello", "attachment_size": 20 * 1024 * 1024}, "超大附件"),
    ]

    for args, description in email_tests:
        print(f"\n{'='*70}")
        print(f"邮件: to={args.get('to')}")
        print(f"说明: {description}")
        print(f"{'='*70}")

        result = await policy.check("send_email", args)

        print(f"\n结果:")
        print(f"  允许执行: {result.allowed}")
        print(f"  原因: {result.reason}")

        if result.validation_result:
            print(f"  验证状态: {result.validation_result.status.value}")
            print(f"  风险等级: {result.validation_result.risk_level.name}")

        input("\n按回车继续...")

    print("\n" + "=" * 70)
    print("参数约束校验器演示结束!")
    print("=" * 70)


async def main():
    """Run all demos."""
    await interactive_approval_demo()
    await custom_validator_demo()
    await parameter_validator_demo()

    print("\n" + "=" * 70)
    print("所有演示完成!")
    print("=" * 70)
    print()
    print("总结:")
    print("1. CommandValidator - 命令行安全检查")
    print("2. FunctionValidator - 自定义函数校验")
    print("3. ParameterValidator - 工具参数约束")
    print("4. CompositeValidator - 组合多个校验器")
    print()


if __name__ == "__main__":
    asyncio.run(main())
