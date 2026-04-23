#!/usr/bin/env python3
"""
示例2: 产物生成与获取
==================

这个示例展示如何在 sandbox 中生成文件（如图片、数据文件）
并获取这些产物。

适用于：生成图表、保存计算结果、创建报告文件
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from framework.sandbox import get_default_sandbox, SandboxConfig


async def main():
    """演示产物生成与获取"""

    print("=" * 60)
    print("示例2: 产物生成与获取")
    print("=" * 60)

    sandbox = get_default_sandbox()

    # 示例2.1: 生成文本文件产物
    print("\n【场景2.1】生成文本报告文件")
    code1 = """
import os

# 获取产物目录（环境变量自动设置）
artifacts_dir = os.environ.get('SANDBOX_ARTIFACTS_DIR', '/tmp/artifacts')
os.makedirs(artifacts_dir, exist_ok=True)

# 生成报告内容
report_lines = [
    "数据分析报告",
    "============",
    "生成时间: 2024-01-01",
    "样本数量: 1000",
    "平均值: 42.5",
    "标准差: 3.2",
    "结论: 数据符合预期"
]
report_content = "\\n".join(report_lines)

# 保存到产物目录
report_path = os.path.join(artifacts_dir, 'analysis_report.txt')
with open(report_path, 'w') as f:
    f.write(report_content)

print(f"报告已生成: {report_path}")
print(f"文件大小: {len(report_content)} 字节")
"""

    result1 = await sandbox.execute(code1)
    print(f"执行成功: {result1.success}")
    print(f"标准输出: {result1.stdout}")

    # 查看产物元数据
    print(f"\n产物列表:")
    for artifact in result1.artifacts:
        print(f"  - {artifact.path} ({artifact.size} bytes, {artifact.mime_type})")

    # 获取产物内容
    if result1.artifacts:
        print("\n【场景2.2】读取产物内容")
        artifacts_content = sandbox.get_artifacts(patterns=["*.txt"])
        for path, content in artifacts_content.items():
            print(f"\n文件: {path}")
            print(f"内容预览: {content[:100]}...")

    # 示例2.2: 生成多个产物文件
    print("\n【场景2.3】生成多个产物文件")
    code2 = """
import os
import json

artifacts_dir = os.environ.get('SANDBOX_ARTIFACTS_DIR', '/tmp/artifacts')
os.makedirs(artifacts_dir, exist_ok=True)

# 生成数据文件
data = {
    "users": [
        {"name": "Alice", "score": 95},
        {"name": "Bob", "score": 87},
        {"name": "Charlie", "score": 92}
    ],
    "metadata": {"total": 3, "average": 91.3}
}

# 保存 JSON 数据
json_path = os.path.join(artifacts_dir, 'data.json')
with open(json_path, 'w') as f:
    json.dump(data, f, indent=2)

# 保存 CSV 数据
csv_path = os.path.join(artifacts_dir, 'data.csv')
with open(csv_path, 'w') as f:
    f.write("name,score\\n")
    for user in data["users"]:
        f.write(f"{user['name']},{user['score']}\\n")

# 保存日志
log_path = os.path.join(artifacts_dir, 'process.log')
with open(log_path, 'w') as f:
    f.write("INFO: Process started\\n")
    f.write("INFO: Data generated\\n")
    f.write("INFO: Process completed\\n")

print("生成了 3 个文件")
print("  - data.json")
print("  - data.csv")
print("  - process.log")
"""

    result2 = await sandbox.execute(code2)
    print(f"执行成功: {result2.success}")
    print(f"\n产物列表:")
    for artifact in result2.artifacts:
        print(f"  - {artifact.path} ({artifact.size} bytes)")

    # 示例2.3: 使用模式过滤获取产物
    print("\n【场景2.4】使用模式过滤获取产物")

    # 只获取 JSON 文件
    json_artifacts = sandbox.get_artifacts(patterns=["*.json"])
    print(f"JSON 文件数量: {len(json_artifacts)}")
    for path, content in json_artifacts.items():
        print(f"\n文件: {path}")
        print(f"内容: {content.decode('utf-8')[:200]}")

    # 获取所有数据文件（JSON 和 CSV）
    data_artifacts = sandbox.get_artifacts(patterns=["*.json", "*.csv"])
    print(f"\n数据文件总数 (JSON + CSV): {len(data_artifacts)}")

    print("\n" + "=" * 60)
    print("示例2完成!")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
