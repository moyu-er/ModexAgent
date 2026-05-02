# Sandbox 使用示例

这个目录包含各种 sandbox 使用场景的示例代码，帮助你快速理解和使用 sandbox 框架。

## 示例列表

### 基础示例

| 示例文件 | 说明 | 适用场景 |
|---------|------|---------|
| `01_basic_python_execution.py` | 基础 Python 代码执行 | 快速测试代码片段、简单计算任务 |
| `02_artifact_generation.py` | 产物生成与获取 | 生成报告、保存数据文件 |
| `03_command_execution.py` | 命令行执行 | 运行系统命令、调用外部工具 |

### 特定 Sandbox 类型示例

| 示例文件 | 说明 | 要求 |
|---------|------|------|
| `04_docker_sandbox.py` | Docker 容器执行 | 本地安装 Docker |
| `05_e2b_sandbox.py` | E2B 云端执行 | 配置 E2B_API_KEY |

### 真实场景示例

| 示例文件 | 说明 | 展示能力 |
|---------|------|---------|
| `06_real_world_scenario.py` | 销售数据分析 | 完整业务场景、多产物处理 |

## 快速开始

### 1. 运行基础示例

```bash
cd /Users/gyt/tool/project/pythonProject/multiDemo/backend/app/framework

# 基础执行
python examples/sandbox/01_basic_python_execution.py

# 产物生成
python examples/sandbox/02_artifact_generation.py

# 命令执行
python examples/sandbox/03_command_execution.py
```

### 2. 运行 Docker 示例（需要 Docker）

```bash
python examples/sandbox/04_docker_sandbox.py
```

### 3. 运行 E2B 示例（需要 API Key）

确保已配置 E2B_API_KEY：

```bash
# 方式1: 环境变量
export E2B_API_KEY="your_api_key"

# 方式2: 配置文件（已配置在 backend/app/config/sandbox.yaml）
```

然后运行：

```bash
python examples/sandbox/05_e2b_sandbox.py
```

### 4. 运行业务场景示例

```bash
python examples/sandbox/06_real_world_scenario.py
```

## 核心概念

### 1. 创建 Sandbox

```python
from framework.sandbox import Sandbox, SandboxType

# 自动选择可用的 sandbox
sandbox = Sandbox.create()

# 指定类型
sandbox = Sandbox.create(SandboxType.SUBPROCESS)
sandbox = Sandbox.create(SandboxType.DOCKER)
sandbox = Sandbox.create(SandboxType.E2B)
```

### 2. 执行 Python 代码

```python
result = await sandbox.execute("print('Hello')")

# 使用自定义配置
config = SandboxConfig(
    enable_validation=True,
    max_execution_time_seconds=30
)
result = await sandbox.execute(code, config=config)
```

### 3. 执行命令

```python
result = await sandbox.execute_command("ls -la")
```

### 4. 生成和获取产物

```python
# 代码中写入产物目录
import os
artifacts_dir = os.environ.get('SANDBOX_ARTIFACTS_DIR', '/tmp/artifacts')
with open(os.path.join(artifacts_dir, 'file.txt'), 'w') as f:
    f.write('content')

# 执行后获取产物
result = await sandbox.execute(code)
for artifact in result.artifacts:
    print(f"产物: {artifact.path}")

# 读取产物内容
artifacts_content = await sandbox.get_artifacts(result.artifacts)
for path, content in artifacts_content.items():
    print(f"{path}: {content}")
```

## 注意事项

1. **SubprocessSandbox**: 本地执行，速度快，但隔离性较弱
2. **DockerSandbox**: 完全隔离，需要 Docker，支持资源限制
3. **E2BSandbox**: 云端执行，最安全，需要 API Key

## 产物目录

各 sandbox 类型的默认产物目录：

- Subprocess: 临时目录/artifacts
- Docker: /app/artifacts
- E2B: /home/user/artifacts

代码中通过环境变量 `SANDBOX_ARTIFACTS_DIR` 获取实际路径。
## Current Runtime Status

Sandbox examples exercise tool execution below the ReAct `ToolNode`. Runtime
approval, cancellation, and control boundaries are summarized in
`docs/current-runtime.md`.
