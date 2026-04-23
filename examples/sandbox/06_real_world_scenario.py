#!/usr/bin/env python3
"""
示例6: 真实业务场景
================

这个示例展示一个完整的业务场景：数据分析任务
- 接收原始数据
- 在 sandbox 中处理数据
- 生成分析结果和可视化数据
- 获取产物文件

适用于：实际业务中的数据处理、报告生成任务
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from framework.sandbox import get_default_sandbox, SandboxConfig


async def analyze_sales_data(sandbox, sales_data):
    """
    业务场景：销售数据分析

    参数:
        sandbox: Sandbox 实例
        sales_data: 销售数据（CSV 格式字符串）

    返回:
        分析结果和产物文件
    """

    # 构建分析代码
    analysis_code = f'''
import os
import json
from collections import defaultdict

# 获取产物目录
artifacts_dir = os.environ.get('SANDBOX_ARTIFACTS_DIR', '/tmp/artifacts')
os.makedirs(artifacts_dir, exist_ok=True)

# 原始数据
sales_data = """{sales_data}"""

# 解析数据
lines = sales_data.strip().split("\\n")
headers = lines[0].split(",")
records = []
for line in lines[1:]:
    values = line.split(",")
    records.append(dict(zip(headers, values)))

print(f"加载了 {{len(records)}} 条销售记录")

# 分析1: 按产品统计销售额
product_sales = defaultdict(float)
for record in records:
    product = record["product"]
    amount = float(record["amount"])
    product_sales[product] += amount

# 分析2: 按地区统计
region_sales = defaultdict(float)
for record in records:
    region = record["region"]
    amount = float(record["amount"])
    region_sales[region] += amount

# 生成分析报告
report = []
report.append("销售数据分析报告")
report.append("=" * 40)
report.append(f"总记录数: {{len(records)}}")
report.append(f"总销售额: ${{sum(product_sales.values()):.2f}}")
report.append("")
report.append("按产品统计:")
for product, total in sorted(product_sales.items(), key=lambda x: -x[1]):
    report.append(f"  {{product}}: ${{total:.2f}}")
report.append("")
report.append("按地区统计:")
for region, total in sorted(region_sales.items(), key=lambda x: -x[1]):
    report.append(f"  {{region}}: ${{total:.2f}}")

report_text = "\\n".join(report)

# 保存报告
report_path = os.path.join(artifacts_dir, "sales_report.txt")
with open(report_path, "w") as f:
    f.write(report_text)

# 保存详细数据（JSON）
json_data = {{
    "summary": {{
        "total_records": len(records),
        "total_sales": sum(product_sales.values()),
        "product_count": len(product_sales),
        "region_count": len(region_sales)
    }},
    "by_product": dict(product_sales),
    "by_region": dict(region_sales),
    "raw_data": records
}}

json_path = os.path.join(artifacts_dir, "sales_data.json")
with open(json_path, "w") as f:
    json.dump(json_data, f, indent=2)

# 保存 CSV 汇总
csv_lines = ["product,total_sales"]
for product, total in sorted(product_sales.items(), key=lambda x: -x[1]):
    csv_lines.append(f"{{product}},{{total}}")

csv_path = os.path.join(artifacts_dir, "product_summary.csv")
with open(csv_path, "w") as f:
    f.write("\\n".join(csv_lines))

print(f"\\n生成了 3 个产物文件:")
print(f"  - sales_report.txt")
print(f"  - sales_data.json")
print(f"  - product_summary.csv")
'''

    # 执行分析
    config = SandboxConfig(
        enable_validation=True,
        max_execution_time_seconds=30
    )

    result = await sandbox.execute(analysis_code, config=config)

    if not result.success:
        print(f"分析失败: {result.error}")
        return None

    print(f"分析完成，耗时: {result.execution_time_ms:.2f}ms")
    print(result.stdout)

    # 获取产物（使用模式匹配所有文件）
    artifacts = sandbox.get_artifacts(patterns=["*"])

    return {
        "success": True,
        "artifacts": artifacts,
        "artifact_meta": result.artifacts
    }


async def main():
    """演示真实业务场景"""

    print("=" * 60)
    print("示例6: 真实业务场景 - 销售数据分析")
    print("=" * 60)

    # 创建 sandbox
    sandbox = get_default_sandbox()

    # 模拟销售数据
    sales_data = """date,product,region,amount
2024-01-01,Laptop,North,1200.00
2024-01-01,Mouse,North,25.00
2024-01-02,Laptop,South,1200.00
2024-01-02,Keyboard,East,75.00
2024-01-03,Monitor,West,300.00
2024-01-03,Laptop,North,1200.00
2024-01-04,Mouse,South,25.00
2024-01-04,Keyboard,West,75.00
2024-01-05,Monitor,North,300.00
2024-01-05,Laptop,East,1200.00"""

    print("\n【场景】分析销售数据")
    print(f"输入数据:\n{sales_data}\n")

    # 执行分析
    result = await analyze_sales_data(sandbox, sales_data)

    if result and result["success"]:
        print("\n【结果】获取分析产物:")

        # 显示报告内容
        if "sales_report.txt" in result["artifacts"]:
            print("\n1. 分析报告内容:")
            report_content = result["artifacts"]["sales_report.txt"].decode("utf-8")
            print(report_content)

        # 显示 JSON 数据摘要
        if "sales_data.json" in result["artifacts"]:
            print("\n2. JSON 数据摘要:")
            import json
            json_data = json.loads(result["artifacts"]["sales_data.json"])
            summary = json_data["summary"]
            print(f"   总记录: {summary['total_records']}")
            print(f"   总销售额: ${summary['total_sales']:.2f}")
            print(f"   产品种类: {summary['product_count']}")
            print(f"   地区数量: {summary['region_count']}")

        # 显示 CSV 内容
        if "product_summary.csv" in result["artifacts"]:
            print("\n3. CSV 汇总:")
            csv_content = result["artifacts"]["product_summary.csv"].decode("utf-8")
            print(csv_content)

    print("\n" + "=" * 60)
    print("示例6完成!")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
