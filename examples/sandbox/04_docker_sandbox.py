#!/usr/bin/env python3
"""
示例4: Docker Sandbox 使用
========================

这个示例展示如何使用 DockerSandbox 在隔离的容器中执行代码。

适用于：需要完全隔离的环境、依赖特定系统库、需要网络隔离
要求：本地必须安装 Docker
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from framework.sandbox import get_sandbox, SandboxConfig, SandboxType


async def main():
    """演示 Docker Sandbox 的使用"""

    print("=" * 60)
    print("示例4: Docker Sandbox 使用")
    print("=" * 60)

    # 检查 Docker 是否可用
    try:
        sandbox = get_sandbox(SandboxType.DOCKER)
        print("\n✓ Docker 可用，继续使用 Docker Sandbox")
    except Exception as e:
        print(f"\n✗ Docker 不可用: {e}")
        print("  请确保 Docker 已安装并正在运行")
        print("  跳过此示例")
        return

    # 示例4.1: 在 Docker 中执行 Python 代码
    print("\n【场景4.1】在隔离容器中执行代码")
    code1 = """
import sys
import os

print(f"Python 版本: {sys.version}")
print(f"运行平台: {sys.platform}")
print(f"当前目录: {os.getcwd()}")
print(f"目录内容: {os.listdir('.')}")

# 检查网络是否隔离（应该无法访问外部网络）
"""

    result1 = await sandbox.execute(code1)
    print(f"执行成功: {result1.success}")
    print(f"输出:\n{result1.stdout}")
    print(f"执行时间: {result1.execution_time_ms:.2f}ms")

    # 示例4.2: 生成产物文件
    print("\n【场景4.2】在容器中生成产物")
    code2 = """
import os

# 获取产物目录
artifacts_dir = os.environ.get('SANDBOX_ARTIFACTS_DIR', '/app/artifacts')
os.makedirs(artifacts_dir, exist_ok=True)

# 生成一些数据
for i in range(3):
    filename = f"file_{i+1}.txt"
    filepath = os.path.join(artifacts_dir, filename)
    with open(filepath, 'w') as f:
        f.write(f"This is file {i+1}\\nGenerated in Docker container\\n")
    print(f"Created: {filename}")

print("\\nAll files created successfully!")
"""

    result2 = await sandbox.execute(code2)
    print(f"执行成功: {result2.success}")
    print(f"输出: {result2.stdout}")
    print(f"\n产物列表:")
    for artifact in result2.artifacts:
        print(f"  - {artifact.path} ({artifact.size} bytes)")

    # 示例4.3: 使用自定义配置
    print("\n【场景4.3】使用自定义配置（内存限制、超时）")

    config = SandboxConfig(
        memory_limit_mb=128,  # 限制 128MB 内存
        max_execution_time_seconds=10,  # 10秒超时
        enable_network=False,  # 禁用网络
    )

    code3 = """
import os

# 尝试分配大量内存（应该会被限制）
data = []
try:
    for i in range(1000):
        data.append("x" * 10000)  # 每次分配 10KB
        if i % 100 == 0:
            print(f"Allocated {i * 10}KB")
except MemoryError:
    print("Memory limit reached!")

print("Script completed")
"""

    result3 = await sandbox.execute(code3, config=config)
    print(f"执行成功: {result3.success}")
    print(f"输出:\n{result3.stdout}")
    if result3.error:
        print(f"错误: {result3.error}")

    # 示例4.4: 执行命令
    print("\n【场景4.4】在 Docker 容器中执行命令")
    result4 = await sandbox.execute_command("uname -a")
    print(f"执行成功: {result4.success}")
    print(f"系统信息: {result4.stdout.strip()}")

    print("\n" + "=" * 60)
    print("示例4完成!")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
