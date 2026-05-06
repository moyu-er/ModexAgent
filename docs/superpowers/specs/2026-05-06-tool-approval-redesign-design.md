# Tool Approval 系统改造设计文档

## 1. 背景与问题

当前 `examples/bot_project/config/bot_config.yml` 中的工具审批机制存在以下问题：

1. **配置层级错误**：审批配置是全局/工具级别的，而非 Agent 级别。每个 Agent 应该独立决定哪些 tool 需要审批。
2. **审批逻辑与 tool 名称强耦合**：当前通过硬编码 `dangerous_tools = ["shell", "write_file", "edit_file"]` 决定审批，而非基于 tool 的执行路径。
3. **路径解析缺陷**：`ArgumentMatcher` 使用 `Path.resolve()` 解析 `.`，结果取决于运行时 `cwd`，而非项目根目录。不支持 `~`（用户主目录）和 `*`（通配符）的正确解析。
4. **造轮子**：路径通配符匹配自行实现，未使用标准库跨平台方案。

## 2. 设计目标

1. **Agent 级审批配置**：每个 Agent（main、peer、subagent）独立配置需要审批的 tool 列表。
2. **路径驱动审批**：审批的核心依据是 tool 操作的路径，而非 tool 名称。未配置的 tool 默认不审批。
3. **跨平台路径解析**：支持 `.`（项目根目录）、`~`（用户主目录）、`*`（通配符），使用标准库 `pathlib` + `fnmatch`。
4. **删除遗留硬编码**：彻底移除 `dangerous_tools` 硬编码列表等旧实现。
5. **框架与示例分离**：通用能力（路径解析、审批分类逻辑）进框架层；业务配置（具体哪些 tool 需要审批）留在示例项目。

## 3. 配置结构设计

### 3.1 YAML 示例（仅 main agent 配置）

```yaml
# ==================== Agent: main ====================
agent:
  system_prompt: |
    你是一个 AI 助手...
  max_iterations: 50

  approval:                     # Agent 级审批配置
    enabled: true
    tools:                      # 需要审批的 tool 列表
      write_file:
        allowed_paths: ["./*"]        # 项目根目录下免审批
      edit_file:
        allowed_paths: ["./*"]
      shell:
        allowed_paths: []             # 空列表 = 所有路径都需审批

# peers / subagent 不配置 approval 节（框架默认不审批）
```

### 3.2 语义规则

| 配置状态 | 行为 |
|---------|------|
| `enabled: false` 或缺少 `approval` 节 | 该 Agent 所有 tool 不审批 |
| `enabled: true` 但 `tools` 为空/缺失 | 该 Agent 所有 tool 不审批（安全默认） |
| `tools.<name>.allowed_paths: []` | 该 tool 所有路径都需审批 |
| `tools.<name>.allowed_paths: ["*"]` | 该 tool 所有路径都免审批 |
| 路径不在 `allowed_paths` 内 | 该 tool 调用需审批 |

### 3.3 路径解析规则

| 写法 | 解析结果 |
|-----|---------|
| `.` 或 `./*` | `bot_service.py` 所在目录（项目根目录） |
| `~` 或 `~/*` | 用户主目录（`Path.home()`） |
| `*` | 通配符，匹配任意路径（通过 `fnmatch`） |
| `./data/*` | 项目根目录下的 `data/` |
| `/absolute/path/*` | 原样保留（绝对路径） |

## 4. 框架层改动

### 4.1 新增配置类

**文件**: `framework/approval/config.py`（新建）

```python
from dataclasses import dataclass, field


@dataclass
class ToolApprovalConfig:
    """单个 tool 的审批配置。"""
    allowed_paths: list[str] = field(default_factory=list)


@dataclass
class AgentApprovalConfig:
    """单个 Agent 的审批配置。"""
    enabled: bool = False
    tools: dict[str, ToolApprovalConfig] = field(default_factory=dict)
```

职责：纯数据结构，承载从 YAML 解析后的审批配置。无业务逻辑。

### 4.2 改造 `TieredToolApprovalClassifier`

**文件**: `framework/agents/react/approval.py`

当前实现使用 `ToolNameMatcher` 匹配 tool 名称（`dangerous_tools` 硬编码列表），改造后改为接收 `AgentApprovalConfig`：

**删除/废弃：**
- `TieredToolApprovalClassifier` 的 `dangerous: ToolNameMatcher`、`sensitive: ToolNameMatcher`、`hardline: ToolNameMatcher` 参数
- 依赖 tool 名称匹配的分类逻辑

**新增逻辑：**

```python
class TieredToolApprovalClassifier:
    def __init__(
        self,
        config: AgentApprovalConfig,
        argument_matcher: ArgumentMatcher | None = None,
    ) -> None:
        self.config = config
        self.argument_matcher = argument_matcher

    def classify(self, tool_call: ToolCall, ctx: AgentContext[Any]) -> str:
        # 1. Agent 审批未启用
        if not self.config.enabled:
            return ApprovalTier.NORMAL

        # 2. 该 tool 未在审批列表中
        tool_config = self.config.tools.get(tool_call.name)
        if tool_config is None:
            return ApprovalTier.NORMAL

        # 3. 提取路径参数并检查
        if self.argument_matcher is not None:
            path_in_allowed = self.argument_matcher.matches(
                tool_call.arguments, tool_config.allowed_paths
            )
            if path_in_allowed:
                return ApprovalTier.NORMAL

        return ApprovalTier.DANGEROUS
```

分类规则：
1. `approval.enabled == false` → `NORMAL`（直接放行）
2. tool 不在 `approval.tools` 中 → `NORMAL`（未配置 = 不审批）
3. 路径匹配 `allowed_paths` → `NORMAL`（白名单内）
4. 路径不匹配 → `DANGEROUS`（需审批）

### 4.3 改造 `ArgumentMatcher`

**文件**: `framework/interceptor/builtin/tool_approval.py`

当前 `ArgumentMatcher` 构造函数接收 `allowed_dirs: list[str]`，内部使用 `Path.resolve()` 解析路径。改造后：

```python
import fnmatch
from pathlib import Path


class ArgumentMatcher:
    def __init__(
        self,
        project_root: Path | None = None,
    ) -> None:
        self.project_root = project_root

    def matches(self, arguments: dict[str, Any], allowed_paths: list[str]) -> bool:
        """检查 arguments 中的路径是否匹配 allowed_paths 中的任一模式。"""
        paths = self._extract_paths(arguments)
        for path in paths:
            resolved = self._resolve_path(path)
            if not self._match_any(resolved, allowed_paths):
                return False
        return True

    def _resolve_path(self, raw: str) -> Path:
        """跨平台路径解析。

        - `.` / `./xxx`  → project_root
        - `~` / `~/xxx`  → Path.home()
        - 绝对路径       → 原样保留
        """
        if raw.startswith("~/"):
            return Path.home() / raw[2:]
        if raw == ".":
            return self.project_root or Path(raw).resolve()
        if raw.startswith("./"):
            root = self.project_root or Path(".").resolve()
            return root / raw[2:]
        return Path(raw).expanduser()

    def _match_any(self, path: Path, patterns: list[str]) -> bool:
        """使用 fnmatch 检查路径是否匹配任一模式。"""
        path_str = str(path)
        for pattern in patterns:
            resolved_pattern = self._resolve_path(pattern)
            if fnmatch.fnmatch(path_str, str(resolved_pattern)):
                return True
        return False

    def _extract_paths(self, arguments: dict[str, Any]) -> list[str]:
        """从 tool 参数中提取路径类参数。"""
        path_keys = {"path", "file_path", "target", "dest", "directory", "dir", "working_dir"}
        paths: list[str] = []
        for key, value in arguments.items():
            if key in path_keys and isinstance(value, str):
                paths.append(value)
        return paths
```

**关键设计点：**
- `fnmatch` 是 Python 标准库，跨平台（Windows/macOS/Linux），支持 `*`、`?`、`[]` 通配符。
- `pathlib.Path` 自动处理路径分隔符差异（`/` vs `\`）。
- `.` 的解析基准是显式传入的 `project_root`，不是 `os.getcwd()`。
- 不引入 `**` 递归通配符（需求已确认不需要）。

### 4.4 `ApprovalRuntime` 组装入口

**文件**: `framework/agents/react/runtime.py` 或 `examples/bot_project/bot/service/core.py`

当前 `RuntimeAssembler` 读取 `config.get("approval", {}).get("dangerous_tools", [...])`。改造后：

```python
# 从 agent 配置中解析 AgentApprovalConfig
approval_config = parse_approval_config(agent_config.get("approval", {}))

# 组装 Runtime
runtime = await RuntimeAssembler.assemble(RuntimeServicesConfig(
    project_root=project_root,          # 新增：项目根目录
    approval_config=approval_config,    # 新增：AgentApprovalConfig
    # ...
))
```

框架层提供 `parse_approval_config()` 工具函数，将 YAML dict 转换为 `AgentApprovalConfig`。

## 5. 示例项目层改动

### 5.1 `bot_config.yml`

仅在 `agent:` 节下新增 `approval:` 配置：

```yaml
agent:
  system_prompt: |
    ...
  max_iterations: 50

  approval:
    enabled: true
    tools:
      write_file:
        allowed_paths: ["./*"]
      edit_file:
        allowed_paths: ["./*"]
      shell:
        allowed_paths: []
```

Peers 和 subagent 不配置 `approval` 节。

### 5.2 `bot_service.py` 传入项目根目录

**文件**: `examples/bot_project/bot/service/core.py`

在 `RuntimeAssembler` 组装时传入 `project_root`：

```python
from pathlib import Path

# 项目根目录 = bot_service.py 所在目录
project_root = Path(__file__).parent.parent

runtime = await RuntimeAssembler.assemble(RuntimeServicesConfig(
    project_root=project_root,
    approval_config=main_agent_approval_config,
    # ...
))
```

## 6. 删除的遗留配置与代码

### 6.1 `bot_config.yml` 中删除

```yaml
# 删除 — 硬编码危险工具列表
approval:
  dangerous_tools: ["shell", "write_file", "edit_file"]

# 删除 — tools.file_tools.allowed_directories 迁移到 agent.approval.tools.<tool>.allowed_paths
tools:
  file_tools:
    allowed_directories:      # ← 删除
      - "."
```

### 6.2 框架代码中删除/废弃

- `TieredToolApprovalClassifier` 的 `dangerous`、`sensitive`、`hardline` `ToolNameMatcher` 参数
- `RuntimeAssembler` 中读取 `approval.dangerous_tools` 的逻辑
- `ArgumentMatcher` 的 `allowed_dirs` 构造函数参数（改为 `project_root` + 按 tool 的 `allowed_paths`）

## 7. 跨平台兼容性

| 平台 | 路径分隔符 | `pathlib` 行为 | `fnmatch` 行为 |
|------|-----------|---------------|---------------|
| Windows | `\` | `Path` 自动处理 | `fnmatch` 使用 `/` 分隔符模式，需统一转换 |
| macOS | `/` | 原生支持 | 原生支持 |
| Linux | `/` | 原生支持 | 原生支持 |

**注意**：`fnmatch` 的模式使用 `/` 作为分隔符。在 Windows 上，需要将 `Path` 的字符串表示中的 `\` 统一替换为 `/` 后再匹配：

```python
def _match_any(self, path: Path, patterns: list[str]) -> bool:
    path_str = str(path).replace("\\", "/")
    for pattern in patterns:
        resolved_pattern = self._resolve_path(pattern)
        pattern_str = str(resolved_pattern).replace("\\", "/")
        if fnmatch.fnmatch(path_str, pattern_str):
            return True
    return False
```

## 8. 测试策略

1. **单元测试**：`tests/unit/approval/`
   - `test_config.py`：验证 `AgentApprovalConfig`、`ToolApprovalConfig` 的解析
   - `test_classifier.py`：验证 `TieredToolApprovalClassifier` 的分类逻辑（enabled=false、tool 未配置、路径匹配/不匹配）
   - `test_argument_matcher.py`：验证路径解析（`.`、`~`、`*`）和跨平台 `fnmatch`

2. **集成测试**：`tests/integration/`
   - 验证 `ReActAgent` 在配置 `approval.enabled=true` 时正确触发 `GraphInterrupt`
   - 验证 resume 后正确执行/拒绝 tool

3. **移除旧测试**：删除涉及 `dangerous_tools` 硬编码列表的测试。

## 9. 接口变更摘要

| 组件 | 变更前 | 变更后 |
|------|-------|-------|
| `TieredToolApprovalClassifier` | `__init__(dangerous=..., sensitive=..., hardline=..., argument_matcher=...)` | `__init__(config=AgentApprovalConfig, argument_matcher=...)` |
| `ArgumentMatcher` | `__init__(allowed_dirs=...)` | `__init__(project_root=...)`；新增 `matches(arguments, allowed_paths)` |
| `RuntimeServicesConfig` | 无 approval 相关字段 | 新增 `project_root: Path`、`approval_config: AgentApprovalConfig` |
| `bot_config.yml` | 无 `agent.approval` | 新增 `agent.approval.enabled`、`agent.approval.tools` |

---

**日期**: 2026-05-06  
**作者**: Claude Code  
**状态**: 待实现
