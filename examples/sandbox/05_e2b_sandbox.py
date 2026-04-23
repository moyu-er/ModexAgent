#!/usr/bin/env python3
"""
示例5: E2B Cloud Sandbox 使用
============================

这个示例展示如何使用 E2BSandbox 在云端执行代码。

E2B 适配器特性:
- 延迟加载: execute() 只返回产物元数据,不自动下载内容
- 按需获取: get_artifacts() 在需要时从远端读取
- 显式下载: download_artifacts() 供用户控制下载时机
- 自动下载: 可通过配置开启自动下载

沙箱生命周期:
- 默认超时: 5分钟
- 执行后保持: 沙箱保持存活以便延迟加载
- 需要清理: 调用 cleanup() 释放资源

适用于: 需要云端隔离环境、不想管理本地 Docker、需要高安全性
要求: 需要配置 E2B_API_KEY 环境变量
"""

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

# 加载配置文件
possible_yaml_paths = [
    Path(__file__).parent.parent.parent / "app" / "config" / "sandbox.yaml",
    Path(__file__).parent.parent.parent.parent / "app" / "config" / "sandbox.yaml",
    Path(__file__).parent.parent.parent.parent.parent / "app" / "config" / "sandbox.yaml",
]

for yaml_path in possible_yaml_paths:
    if yaml_path.exists():
        print(f"Loading config from: {yaml_path}")
        with open(yaml_path, "r") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and ":" in line:
                    key, value = line.split(":", 1)
                    key = key.strip()
                    value = value.strip().strip('"').strip("'")
                    if key and value and key not in os.environ:
                        os.environ[key] = value
        break

from framework.sandbox import get_sandbox, SandboxConfig, SandboxType


async def main():
    """演示 E2B Cloud Sandbox 的使用"""

    print("=" * 60)
    print("示例5: E2B Cloud Sandbox 使用")
    print("=" * 60)

    # 检查 E2B API Key 是否配置
    api_key = os.environ.get("E2B_API_KEY")
    if not api_key:
        print("\n✗ E2B_API_KEY 未配置")
        print("  请设置环境变量: export E2B_API_KEY='your_api_key'")
        print("  或在 backend/app/config/sandbox.yaml 中配置")
        print("  跳过此示例")
        return

    print(f"\n✓ E2B API Key 已配置 ({api_key[:10]}...)")

    try:
        sandbox = get_sandbox(SandboxType.E2B)
        print("✓ E2B Sandbox 创建成功")
    except Exception as e:
        print(f"✗ 创建 E2B Sandbox 失败: {e}")
        return

    # 示例5.1: 在云端执行代码
    print("\n【场景5.1】在云端执行 Python 代码")
    code1 = """
import sys
import os
import platform

print("=== 云端环境信息 ===")
print(f"Python 版本: {sys.version}")
print(f"平台: {platform.platform()}")
print(f"当前目录: {os.getcwd()}")

# 简单的计算
result = sum(range(1, 101))
print(f"\\n计算结果: 1+2+...+100 = {result}")
"""

    result1 = await sandbox.execute(code1)
    print(f"执行成功: {result1.success}")
    print(f"输出:\n{result1.stdout}")
    print(f"执行时间: {result1.execution_time_ms:.2f}ms")

    # 示例5.2: 生成产物文件 (延迟加载模式)
    print("\n【场景5.2】在云端生成产物文件 (延迟加载模式)")
    print("  说明: execute() 只返回产物元数据,不自动下载内容")
    
    code2 = """
import os

# E2B 的产物目录
artifacts_dir = os.environ.get('SANDBOX_ARTIFACTS_DIR', '/home/user/artifacts')
os.makedirs(artifacts_dir, exist_ok=True)

# 生成报告
report_lines = [
    "云端执行报告",
    "============",
    "执行环境: E2B Cloud Sandbox",
    "执行状态: 成功",
    "生成时间: 2024",
    "",
    "总结: 代码在云端安全环境中成功执行!"
]
report = "\\n".join(report_lines)

report_path = os.path.join(artifacts_dir, 'cloud_report.txt')
with open(report_path, 'w') as f:
    f.write(report)

print(f"报告已保存到: {report_path}")
print(f"文件大小: {len(report)} 字符")
"""

    result2 = await sandbox.execute(code2)
    print(f"执行成功: {result2.success}")
    print(f"输出: {result2.stdout}")

    # 查看产物元数据
    if result2.artifacts:
        print(f"\n云端产物元数据 (仅元数据,未下载内容):")
        for artifact in result2.artifacts:
            print(f"  - {artifact.path} ({artifact.size} bytes, {artifact.mime_type})")

        # 【场景5.3】延迟加载产物内容
        print("\n【场景5.3】延迟加载产物内容")
        print("  说明: 使用 get_artifacts() 按需从远端获取内容")
        print("        沙箱保持存活,可以多次获取")

        artifacts_content = sandbox.get_artifacts(patterns=["*"])
        for path, content in artifacts_content.items():
            print(f"\n  文件: {path}")
            print(f"  大小: {len(content)} bytes")
            print(f"  内容:\n{content.decode('utf-8')}")

        # 【场景5.4】显式下载产物到本地
        print("\n【场景5.4】显式下载产物到本地")
        print("  说明: 使用 download_artifacts() 保存到指定目录")

        local_output_dir = Path(__file__).parent / "e2b_downloads"
        
        try:
            downloaded = sandbox.download_artifacts(
                patterns=["*"],
                local_dir=str(local_output_dir)
            )

            for name, local_path in downloaded.items():
                print(f"  ✓ 已下载: {name} -> {local_path}")

            # 验证本地文件
            print("\n  验证本地文件:")
            for name, local_path in downloaded.items():
                path = Path(local_path)
                if path.exists():
                    size = path.stat().st_size
                    print(f"    ✓ {name}: {size} bytes")
                else:
                    print(f"    ✗ {name}: 文件不存在")
        except Exception as e:
            print(f"  ✗ 下载失败: {e}")

    # 示例5.5: 自动下载模式
    print("\n【场景5.5】自动下载模式")
    print("  说明: 通过配置开启自动下载,execute() 后自动保存产物")

    auto_download_config = SandboxConfig(
        auto_download_artifacts=True,
        auto_download_patterns=["*.txt"]
    )

    code3 = """
import os

artifacts_dir = os.environ.get('SANDBOX_ARTIFACTS_DIR', '/home/user/artifacts')
os.makedirs(artifacts_dir, exist_ok=True)

# 生成多个文件
with open(os.path.join(artifacts_dir, 'auto_report.txt'), 'w') as f:
    f.write('This is auto-downloaded report')

with open(os.path.join(artifacts_dir, 'data.json'), 'w') as f:
    f.write('{"key": "value"}')

print('Files created: auto_report.txt, data.json')
"""

    result3 = await sandbox.execute(code3, config=auto_download_config)
    print(f"执行成功: {result3.success}")
    print(f"输出: {result3.stdout}")
    print("  产物已自动下载到本地 artifacts 目录 (仅匹配 *.txt 的文件)")

    # 示例5.6: 执行命令
    print("\n【场景5.6】在云端执行命令")
    result4 = await sandbox.execute_command("echo 'Hello from E2B cloud!' && pwd && ls -la")
    print(f"执行成功: {result4.success}")
    print(f"输出:\n{result4.stdout}")

    # 示例5.7: 沙箱超时演示
    print("\n【场景5.7】沙箱超时处理")
    print("  说明: 沙箱默认5分钟超时,超时后无法获取产物")
    
    code4 = """
import os

artifacts_dir = os.environ.get('SANDBOX_ARTIFACTS_DIR', '/home/user/artifacts')
os.makedirs(artifacts_dir, exist_ok=True)

with open(os.path.join(artifacts_dir, 'timeout_test.txt'), 'w') as f:
    f.write('This file may not be accessible after timeout')

print('File created')
"""

    result5 = await sandbox.execute(code4)
    print(f"执行成功: {result5.success}")
    
    # 此时沙箱仍然存活,可以获取产物
    try:
        content = sandbox.get_artifacts(["timeout_test.txt"])
        if content:
            print("  ✓ 沙箱存活,成功获取产物")
        else:
            print("  ⚠ 产物为空")
    except Exception as e:
        print(f"  ✗ 获取失败: {e}")

    # 示例5.8: 清理资源
    print("\n【场景5.8】清理资源")
    print("  说明: 调用 cleanup() 释放沙箱资源")
    await sandbox.cleanup()
    print("  ✓ 沙箱已清理")

    # 清理后尝试获取产物会失败
    try:
        content = sandbox.get_artifacts(["*"])
        print(f"  ⚠ 清理后获取产物: {len(content)} 个文件 (可能来自缓存)")
    except Exception as e:
        print(f"  ✓ 清理后无法获取产物 (符合预期): {e}")

    print("\n" + "=" * 60)
    print("示例5完成!")
    print("=" * 60)
    print("\n关键要点:")
    print("1. execute() 只返回产物元数据,不自动下载")
    print("2. get_artifacts() 按需从远端获取内容")
    print("3. download_artifacts() 显式保存到本地")
    print("4. 沙箱默认5分钟超时,超时后无法获取产物")
    print("5. 调用 cleanup() 及时释放资源")


if __name__ == "__main__":
    asyncio.run(main())
