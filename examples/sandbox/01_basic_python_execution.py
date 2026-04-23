#!/usr/bin/env python3
"""
示例1: 基础 Python 代码执行
=======================

这个示例展示最基本的 sandbox 用法：执行 Python 代码并获取结果。
适用于：快速测试代码片段、运行简单的计算任务
"""

import asyncio
import sys
from pathlib import Path

# 添加项目根目录到路径
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from framework.sandbox import get_default_sandbox, SandboxConfig


async def main():
    """演示基础 Python 代码执行"""

    print("=" * 60)
    print("示例1: 基础 Python 代码执行")
    print("=" * 60)

    # 创建 sandbox 实例（默认使用 subprocess）
    sandbox = get_default_sandbox()

    # 示例1.1: 执行简单的计算
    print("\n【场景1.1】执行简单计算")
    code1 = """
# 计算斐波那契数列
a, b = 0, 1
result = []
for _ in range(10):
    result.append(a)
    a, b = b, a + b
print(f"斐波那契数列(前10项): {result}")
print(f"总和: {sum(result)}")
"""

    result1 = await sandbox.execute(code1)
    print(f"执行成功: {result1.success}")
    print(f"标准输出:\n{result1.stdout}")
    print(f"执行时间: {result1.execution_time_ms:.2f}ms")

    # 示例1.2: 执行有语法错误的代码（触发验证）
    print("\n【场景1.2】执行有语法错误的代码")
    code2 = """
print("开始")
if True
    print("这行有缩进错误")
"""

    result2 = await sandbox.execute(code2)
    print(f"执行成功: {result2.success}")
    print(f"错误信息: {result2.error}")
    # 注意：验证错误时 exit_code 为 None
    print(f"退出码: {result2.exit_code}")

    # 示例1.3: 禁用验证直接执行
    print("\n【场景1.3】禁用语法验证")
    config = SandboxConfig(enable_validation=False)
    result3 = await sandbox.execute(code2, config=config)
    print(f"执行成功: {result3.success}")
    print(f"错误信息: {result3.error}")
    # 运行时错误会有 exit_code
    print(f"退出码: {result3.exit_code}")

    print("\n" + "=" * 60)
    print("示例1完成!")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
