# Tool Approval 系统改造实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将工具审批机制从全局/工具级别改为 Agent 级别，基于路径驱动审批，删除硬编码，支持跨平台路径解析。

**Architecture:** 新增 `AgentApprovalConfig` / `ToolApprovalConfig` 配置类；改造 `TieredToolApprovalClassifier` 从 tool 名称匹配转向路径匹配；改造 `ArgumentMatcher` 支持 `.` / `~` / `*` 解析；示例项目通过 `RuntimeServicesConfig` 传入项目根目录和审批配置。

**Tech Stack:** Python 3.11+, pathlib, fnmatch, pytest

---

## 文件变更总览

| 文件 | 操作 | 职责 |
|------|------|------|
| `framework/approval/config.py` | 新建 | 审批配置数据结构（AgentApprovalConfig, ToolApprovalConfig） |
| `framework/approval/__init__.py` | 修改 | 导出新配置类 |
| `framework/agents/react/approval.py` | 修改 | 改造 TieredToolApprovalClassifier，删除 ToolNameMatcher 依赖 |
| `framework/interceptor/builtin/tool_approval.py` | 修改 | 改造 ArgumentMatcher，支持 project_root 和 fnmatch |
| `framework/agents/react/assembler.py` | 修改 | RuntimeServicesConfig 新增 project_root 字段 |
| `examples/bot_project/bot/service/core.py` | 修改 | _assemble_runtime() 读取 agent.approval 配置，删除 dangerous_tools 硬编码 |
| `examples/bot_project/config/bot_config.yml` | 修改 | 新增 agent.approval 配置，删除 tools.file_tools.allowed_directories |
| `tests/unit/approval/test_config.py` | 新建 | 配置类解析测试 |
| `tests/unit/test_argument_matcher.py` | 新建 | ArgumentMatcher 路径解析和匹配测试 |
| `tests/unit/agents/react/test_approval.py` | 修改 | 更新 TieredToolApprovalClassifier 测试 |
| `tests/unit/test_tiered_tool_approval.py` | 修改 | 更新/删除涉及旧 matcher 的测试 |

---

## Task 1: 新增审批配置类

**Files:**
- Create: `framework/approval/config.py`
- Modify: `framework/approval/__init__.py`

- [ ] **Step 1: 创建 `framework/approval/config.py`**

```python
"""Approval configuration dataclasses."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ToolApprovalConfig:
    """Per-tool approval configuration.

    allowed_paths: list of path patterns that do NOT require approval.
    Empty list means ALL paths require approval.
    ["*"] means NO paths require approval.
    """
    allowed_paths: list[str] = field(default_factory=list)


@dataclass
class AgentApprovalConfig:
    """Per-agent approval configuration.

    enabled: whether approval checking is active for this agent.
    tools: mapping from tool name to its approval config.
        Tools not in this mapping are NOT subject to approval.
    """
    enabled: bool = False
    tools: dict[str, ToolApprovalConfig] = field(default_factory=dict)
```

- [ ] **Step 2: 更新 `framework/approval/__init__.py` 导出新类**

在现有导入后追加：

```python
from .config import AgentApprovalConfig, ToolApprovalConfig

__all__ = [
    # ... existing entries ...
    "AgentApprovalConfig",
    "ToolApprovalConfig",
]
```

- [ ] **Step 3: 运行现有 approval 测试确保未破坏**

```bash
cd F:/tool/pythonProject/ModexAgent
pytest tests/unit/approval/ -v
```

Expected: all existing tests pass.

- [ ] **Step 4: Commit**

```bash
git add framework/approval/config.py framework/approval/__init__.py
git commit -m "feat(approval): add AgentApprovalConfig and ToolApprovalConfig dataclasses"
```

---

## Task 2: 改造 ArgumentMatcher

**Files:**
- Modify: `framework/interceptor/builtin/tool_approval.py`
- Create: `tests/unit/test_argument_matcher.py`

- [ ] **Step 1: 编写 ArgumentMatcher 测试（先写失败测试）**

```python
"""Tests for ArgumentMatcher path resolution and matching."""
import fnmatch
from pathlib import Path

import pytest

from framework.interceptor.builtin.tool_approval import ArgumentMatcher


class TestResolvePath:
    """Test _resolve_path handles ., ~, and absolute paths."""

    def test_dot_resolves_to_project_root(self):
        root = Path("/project")
        matcher = ArgumentMatcher(project_root=root)
        result = matcher._resolve_path(".")
        assert result == root

    def test_dot_slash_resolves_to_project_root_subpath(self):
        root = Path("/project")
        matcher = ArgumentMatcher(project_root=root)
        result = matcher._resolve_path("./data")
        assert result == Path("/project/data")

    def test_tilde_resolves_to_home(self):
        matcher = ArgumentMatcher(project_root=Path("/project"))
        result = matcher._resolve_path("~/Documents")
        assert result == Path.home() / "Documents"

    def test_absolute_path_unchanged(self):
        matcher = ArgumentMatcher(project_root=Path("/project"))
        result = matcher._resolve_path("/etc/passwd")
        assert result == Path("/etc/passwd")


class TestMatchAny:
    """Test _match_any with fnmatch patterns."""

    def test_star_matches_any(self):
        matcher = ArgumentMatcher(project_root=Path("/project"))
        assert matcher._match_any(Path("/any/path"), ["*"]) is True

    def test_specific_pattern_matches(self):
        matcher = ArgumentMatcher(project_root=Path("/project"))
        assert matcher._match_any(Path("/project/data"), ["/project/*"]) is True

    def test_no_match_returns_false(self):
        matcher = ArgumentMatcher(project_root=Path("/project"))
        assert matcher._match_any(Path("/outside"), ["/project/*"]) is False

    def test_multiple_patterns_or_logic(self):
        matcher = ArgumentMatcher(project_root=Path("/project"))
        assert matcher._match_any(Path("/tmp"), ["/project/*", "/tmp"]) is True


class TestMatches:
    """Test matches() with tool arguments."""

    def test_path_in_allowed_paths(self):
        matcher = ArgumentMatcher(project_root=Path("/project"))
        args = {"path": "./file.txt"}
        assert matcher.matches(args, ["./*"]) is True

    def test_path_not_in_allowed_paths(self):
        matcher = ArgumentMatcher(project_root=Path("/project"))
        args = {"path": "/etc/passwd"}
        assert matcher.matches(args, ["./*"]) is False

    def test_no_path_argument_returns_true(self):
        matcher = ArgumentMatcher(project_root=Path("/project"))
        args = {"content": "hello"}
        assert matcher.matches(args, ["./*"]) is True

    def test_working_dir_argument_extracted(self):
        matcher = ArgumentMatcher(project_root=Path("/project"))
        args = {"command": "ls", "working_dir": "."}
        assert matcher.matches(args, ["./*"]) is True
```

- [ ] **Step 2: 运行测试确认失败**

```bash
pytest tests/unit/test_argument_matcher.py -v
```

Expected: FAIL — ArgumentMatcher 尚未改造，缺少 `project_root` 参数和 `matches()` 方法。

- [ ] **Step 3: 改造 ArgumentMatcher**

修改 `framework/interceptor/builtin/tool_approval.py` 中的 `ArgumentMatcher` 类：

```python
import fnmatch


class ArgumentMatcher:
    """Match tool arguments against allowed path patterns for approval.

    Uses fnmatch for cross-platform wildcard support (*, ?, []).
    Resolves . to project_root, ~ to user home.
    """

    def __init__(self, project_root: Path | None = None) -> None:
        self.project_root = project_root

    def matches(self, arguments: dict[str, Any], allowed_paths: list[str]) -> bool:
        """Returns True if all path arguments match at least one allowed pattern."""
        paths = self._extract_paths(arguments)
        if not paths:
            return True  # No path arguments — nothing to check
        for path in paths:
            resolved = self._resolve_path(path)
            if not self._match_any(resolved, allowed_paths):
                return False
        return True

    def _resolve_path(self, raw: str) -> Path:
        """Resolve special path prefixes.

        - . / ./xxx  → project_root (or cwd if project_root not set)
        - ~ / ~/xxx  → Path.home()
        - absolute   → as-is
        """
        if raw.startswith("~/"):
            return Path.home() / raw[2:]
        if raw == ".":
            return self.project_root if self.project_root is not None else Path(".").resolve()
        if raw.startswith("./"):
            root = self.project_root if self.project_root is not None else Path(".").resolve()
            return root / raw[2:]
        return Path(raw).expanduser()

    def _match_any(self, path: Path, patterns: list[str]) -> bool:
        """Check if path matches any pattern using fnmatch.

        Normalizes Windows backslashes to forward slashes for fnmatch.
        """
        path_str = str(path).replace("\\", "/")
        for pattern in patterns:
            resolved_pattern = self._resolve_path(pattern)
            pattern_str = str(resolved_pattern).replace("\\", "/")
            if fnmatch.fnmatch(path_str, pattern_str):
                return True
        return False

    def _extract_paths(self, arguments: dict[str, Any]) -> list[str]:
        """Extract path-like values from tool arguments."""
        path_keys = {
            "path", "file_path", "target", "dest",
            "directory", "dir", "working_dir",
        }
        paths: list[str] = []
        for key, value in arguments.items():
            if key in path_keys and isinstance(value, str):
                paths.append(value)
        return paths

    # ---- Legacy API (retained for backward compat during migration) ----

    def is_allowed(self, tool_call) -> bool:
        """Legacy API — delegates to matches() with empty allowed_paths.

        This returns False (not allowed) when no paths match, which in the
        old API meant "needs approval" (dangerous). Kept for interceptor use.
        """
        args = tool_call.arguments or {}
        # Old behavior: check against allowed_dirs passed to __init__
        # Since we removed allowed_dirs, this method is no longer used
        # by the new flow. Mark for removal in a follow-up.
        return self.matches(args, [])  # type: ignore[return-value]
```

**注意：** 保留 `is_allowed()` 方法但标记为遗留，防止 `TieredToolApprovalInterceptor` 在过渡期间报错。实际新流程使用 `matches()`。

- [ ] **Step 4: 运行测试确认通过**

```bash
pytest tests/unit/test_argument_matcher.py -v
```

Expected: all 10 tests pass.

- [ ] **Step 5: Commit**

```bash
git add framework/interceptor/builtin/tool_approval.py tests/unit/test_argument_matcher.py
git commit -m "feat(approval): rewrite ArgumentMatcher with project_root, fnmatch, cross-platform paths"
```

---

## Task 3: 改造 TieredToolApprovalClassifier

**Files:**
- Modify: `framework/agents/react/approval.py`
- Modify: `tests/unit/agents/react/test_approval.py`

- [ ] **Step 1: 更新测试文件 `tests/unit/agents/react/test_approval.py`**

替换原有测试为新分类逻辑测试：

```python
"""Tests for ApprovalRuntime, ApprovalClassifier, TieredToolApprovalClassifier."""
import pytest
from framework.agents.react.approval import (
    ApprovalClassifier,
    TieredToolApprovalClassifier,
    ApprovalRuntime,
)
from framework.approval.config import AgentApprovalConfig, ToolApprovalConfig
from framework.approval.constants import ApprovalTier
from framework.interceptor.builtin.tool_approval import ArgumentMatcher
from framework.core.types import ToolCall
from framework.core.agent import AgentContext
from framework.core.tool_manager import InMemoryToolManager
from framework.memory.history import ListMessageHistory


def make_ctx():
    return AgentContext(
        system_prompt="test",
        history=ListMessageHistory(),
        tool_manager=InMemoryToolManager(),
    )


class TestTieredToolApprovalClassifier:
    def test_disabled_returns_normal(self):
        config = AgentApprovalConfig(enabled=False)
        c = TieredToolApprovalClassifier(config=config)
        tc = ToolCall(tool_name="shell", call_id="1", arguments={})
        assert c.classify(tc, make_ctx()) == ApprovalTier.NORMAL

    def test_tool_not_in_config_returns_normal(self):
        config = AgentApprovalConfig(
            enabled=True,
            tools={"write_file": ToolApprovalConfig(allowed_paths=["./*"])},
        )
        c = TieredToolApprovalClassifier(config=config)
        tc = ToolCall(tool_name="shell", call_id="1", arguments={})
        assert c.classify(tc, make_ctx()) == ApprovalTier.NORMAL

    def test_path_in_allowed_returns_normal(self):
        config = AgentApprovalConfig(
            enabled=True,
            tools={"write_file": ToolApprovalConfig(allowed_paths=["./*"])},
        )
        matcher = ArgumentMatcher(project_root=Path("/project"))
        c = TieredToolApprovalClassifier(config=config, argument_matcher=matcher)
        tc = ToolCall(tool_name="write_file", call_id="1", arguments={"path": "./file.txt"})
        assert c.classify(tc, make_ctx()) == ApprovalTier.NORMAL

    def test_path_not_in_allowed_returns_dangerous(self):
        config = AgentApprovalConfig(
            enabled=True,
            tools={"write_file": ToolApprovalConfig(allowed_paths=["./*"])},
        )
        matcher = ArgumentMatcher(project_root=Path("/project"))
        c = TieredToolApprovalClassifier(config=config, argument_matcher=matcher)
        tc = ToolCall(tool_name="write_file", call_id="1", arguments={"path": "/etc/passwd"})
        assert c.classify(tc, make_ctx()) == ApprovalTier.DANGEROUS

    def test_empty_allowed_paths_all_dangerous(self):
        config = AgentApprovalConfig(
            enabled=True,
            tools={"shell": ToolApprovalConfig(allowed_paths=[])},
        )
        matcher = ArgumentMatcher(project_root=Path("/project"))
        c = TieredToolApprovalClassifier(config=config, argument_matcher=matcher)
        tc = ToolCall(tool_name="shell", call_id="1", arguments={"command": "ls"})
        assert c.classify(tc, make_ctx()) == ApprovalTier.DANGEROUS

    def test_star_allowed_paths_all_normal(self):
        config = AgentApprovalConfig(
            enabled=True,
            tools={"shell": ToolApprovalConfig(allowed_paths=["*"])},
        )
        matcher = ArgumentMatcher(project_root=Path("/project"))
        c = TieredToolApprovalClassifier(config=config, argument_matcher=matcher)
        tc = ToolCall(tool_name="shell", call_id="1", arguments={"command": "ls"})
        assert c.classify(tc, make_ctx()) == ApprovalTier.NORMAL


class TestApprovalRuntime:
    def test_construction(self):
        from framework.agents.react.strategy import InlineWaitStrategy
        from framework.control.channel import InMemoryControlChannel
        config = AgentApprovalConfig()
        classifier = TieredToolApprovalClassifier(config=config)
        strategy = InlineWaitStrategy(InMemoryControlChannel())
        ar = ApprovalRuntime(classifier=classifier, suspend_strategy=strategy)
        assert ar.classifier is classifier
        assert ar.suspend_strategy is strategy
        assert ar.deny_as_cancel is True
```

- [ ] **Step 2: 运行测试确认失败**

```bash
pytest tests/unit/agents/react/test_approval.py -v
```

Expected: FAIL — TieredToolApprovalClassifier 尚未改造，缺少 `config` 参数。

- [ ] **Step 3: 改造 TieredToolApprovalClassifier**

修改 `framework/agents/react/approval.py`：

```python
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from framework.approval.config import AgentApprovalConfig
from framework.approval.constants import ApprovalTier
from framework.interceptor.builtin.tool_approval import ArgumentMatcher

if TYPE_CHECKING:
    from framework.agents.react.strategy import SuspendStrategy
    from framework.core.agent import AgentContext
    from framework.core.types import ToolCall


class ApprovalClassifier(Protocol):
    def classify(self, tool_call: ToolCall, ctx: AgentContext[Any]) -> str: ...


@dataclass
class TieredToolApprovalClassifier:
    """Agent-level tool approval classifier driven by path rules.

    Replaces the old name-based matching (hardline/dangerous/sensitive ToolNameMatcher)
    with a configuration-driven approach:
    - approval.enabled=False  → all tools NORMAL
    - tool not in config      → NORMAL
    - path matches allowed    → NORMAL
    - path does not match     → DANGEROUS
    """
    config: AgentApprovalConfig
    argument_matcher: ArgumentMatcher | None = None

    def classify(self, tool_call: ToolCall, ctx: AgentContext[Any]) -> str:
        # 1. Approval disabled for this agent
        if not self.config.enabled:
            return ApprovalTier.NORMAL

        # 2. Tool not configured for approval
        tool_config = self.config.tools.get(tool_call.name)
        if tool_config is None:
            return ApprovalTier.NORMAL

        # 3. Check path arguments against allowed_paths
        if self.argument_matcher is not None:
            path_allowed = self.argument_matcher.matches(
                tool_call.arguments or {},
                tool_config.allowed_paths,
            )
            if path_allowed:
                return ApprovalTier.NORMAL

        return ApprovalTier.DANGEROUS


@dataclass
class ApprovalRuntime:
    classifier: ApprovalClassifier
    suspend_strategy: SuspendStrategy
    deny_as_cancel: bool = True
```

- [ ] **Step 4: 运行测试确认通过**

```bash
pytest tests/unit/agents/react/test_approval.py -v
```

Expected: all 7 tests pass.

- [ ] **Step 5: Commit**

```bash
git add framework/agents/react/approval.py tests/unit/agents/react/test_approval.py
git commit -m "feat(approval): rewrite TieredToolApprovalClassifier with AgentApprovalConfig"
```

---

## Task 4: 更新 RuntimeServicesConfig 和 RuntimeAssembler

**Files:**
- Modify: `framework/agents/react/assembler.py`

- [ ] **Step 1: 修改 `framework/agents/react/assembler.py`**

在 `RuntimeServicesConfig` 中新增 `project_root` 字段：

```python
from pathlib import Path


@dataclass
class RuntimeServicesConfig:
    """Runtime 装配配置 — 框架通用, 不含业务特化逻辑。"""

    mode: Literal["clean", "full"] = "full"
    hooks: HookRunner | None = None
    interceptors: list[Interceptor] | None = None
    approval_classifier: Any = None
    approval_strategy: Any = None
    project_root: Path | None = None       # NEW: for ArgumentMatcher path resolution
    control_channel: ControlChannel | None = None
    control_store: ControlStore | None = None
    command_handlers: list[tuple[ControlCommandType, Any]] | None = None
    checkpoint_store: Any = None
    injection_queue: asyncio.Queue[str] | None = None
    governance: Any = None
    safety: Any = None
```

`RuntimeAssembler.assemble()` 方法不需要修改（它只透传字段，不直接消费 `project_root`）。

- [ ] **Step 2: 运行现有 assembler 测试**

```bash
pytest tests/unit/agents/react/test_assembler.py -v 2>/dev/null || echo "no assembler test file"
pytest tests/unit/ -k assembler -v 2>/dev/null || echo "no assembler tests"
```

Expected: no failures (assembler tests may not exist; existing tests should still pass).

- [ ] **Step 3: Commit**

```bash
git add framework/agents/react/assembler.py
git commit -m "feat(runtime): add project_root to RuntimeServicesConfig"
```

---

## Task 5: 改造示例项目 core.py

**Files:**
- Modify: `examples/bot_project/bot/service/core.py`
- Modify: `examples/bot_project/config/bot_config.yml`

- [ ] **Step 1: 修改 `_assemble_runtime()` 方法**

在 `examples/bot_project/bot/service/core.py` 中，替换 `_assemble_runtime()` 的审批相关逻辑：

```python
    async def _assemble_runtime(self, hooks: Any = None) -> Any:
        """Build ReActRuntime via framework RuntimeAssembler."""
        from framework.agents.react.assembler import RuntimeAssembler, RuntimeServicesConfig
        from framework.agents.react.approval import TieredToolApprovalClassifier
        from framework.agents.react.state import StateStoreTurnResumeStateStore
        from framework.agents.react.strategy import SuspendResumeStrategy
        from framework.approval.config import AgentApprovalConfig, ToolApprovalConfig
        from framework.approval.store import LocalFileApprovalStateStore
        from framework.control.store import InMemoryControlStore
        from framework.control.types import ControlCommandType
        from framework.interceptor.builtin.tool_approval import ArgumentMatcher
        from framework.interceptor.handler import DefaultCancelHandler

        # Parse agent-level approval config from bot_config.yml
        agent_config = self.config.get("agent", {})
        approval_raw = agent_config.get("approval", {})
        approval_enabled = approval_raw.get("enabled", False)
        approval_tools_raw = approval_raw.get("tools", {})

        tools_approval: dict[str, ToolApprovalConfig] = {}
        for tool_name, tool_cfg in approval_tools_raw.items():
            allowed_paths = tool_cfg.get("allowed_paths", []) if isinstance(tool_cfg, dict) else []
            tools_approval[tool_name] = ToolApprovalConfig(allowed_paths=allowed_paths)

        approval_config = AgentApprovalConfig(
            enabled=approval_enabled,
            tools=tools_approval,
        )

        project_root = self._project_dir

        runtime = await RuntimeAssembler.assemble(RuntimeServicesConfig(
            mode="full",
            hooks=hooks,
            interceptors=list(self.interceptor_chain.interceptors) if self.interceptor_chain else None,
            project_root=project_root,                       # NEW
            approval_classifier=TieredToolApprovalClassifier(
                config=approval_config,
                argument_matcher=ArgumentMatcher(project_root=project_root),
            ),
            approval_strategy=SuspendResumeStrategy(
                LocalFileApprovalStateStore(self._approval_workspace),
                StateStoreTurnResumeStateStore(self._checkpoint_store),
            ),
            control_channel=self.control_channel,
            control_store=InMemoryControlStore(),
            command_handlers=[(ControlCommandType.CANCEL_TURN, DefaultCancelHandler())],
            checkpoint_store=self._checkpoint_store,
            governance=self._build_governance(),
            safety=self.safety_policy,
        ))
        print(f"[OK] ReActRuntime built (approval_enabled={approval_enabled}, tools={list(tools_approval.keys())})")
        return runtime
```

同时删除顶部 import 中不再需要的 `ToolNameMatcher`：

```python
# REMOVE this import:
# from framework.interceptor.builtin.tool_approval import (
#     ArgumentMatcher,
#     ToolNameMatcher,
# )

# KEEP only ArgumentMatcher (moved into _assemble_runtime local import or keep at top)
from framework.interceptor.builtin.tool_approval import ArgumentMatcher
```

- [ ] **Step 2: 修改 `bot_config.yml`**

在 `agent:` 节下新增 `approval:` 配置：

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

删除 `tools.file_tools.allowed_directories`：

```yaml
tools:
  file_tools:
    enabled: true
    # DELETE: allowed_directories:
    # DELETE:   - "."

  shell_tools:
    enabled: true
    timeout: 60
    working_dir: null
    enable_safety_guard: false
    restrict_to_workspace: true
```

- [ ] **Step 3: 运行示例项目配置加载测试**

```bash
cd F:/tool/pythonProject/ModexAgent
python -c "
from pathlib import Path
import yaml
config_path = Path('examples/bot_project/config/bot_config.yml')
config = yaml.safe_load(config_path.read_text(encoding='utf-8'))
approval = config['agent'].get('approval', {})
print('approval enabled:', approval.get('enabled'))
print('tools:', list(approval.get('tools', {}).keys()))
"
```

Expected:
```
approval enabled: True
tools: ['write_file', 'edit_file', 'shell']
```

- [ ] **Step 4: Commit**

```bash
git add examples/bot_project/bot/service/core.py examples/bot_project/config/bot_config.yml
git commit -m "feat(bot): agent-level approval config with path-based rules"
```

---

## Task 6: 清理旧测试和遗留代码

**Files:**
- Modify: `tests/unit/test_tiered_tool_approval.py`

- [ ] **Step 1: 更新 `tests/unit/test_tiered_tool_approval.py`**

该文件测试 `TieredToolApprovalInterceptor`，不是 `TieredToolApprovalClassifier`。但它使用了旧的 `ToolNameMatcher` API。需要确认这些测试是否仍然有效。

检查 `TieredToolApprovalInterceptor` 是否仍需要 `hardline_matcher`、`dangerous_matcher`、`sensitive_matcher`：

查看 `framework/interceptor/builtin/tool_approval.py` 中的 `TieredToolApprovalInterceptor.__init__`——它仍然接收这些 matcher 参数。这意味着 **Interceptor 层的旧 API 暂时保留**，只有 Classifier 层被改造。

因此 `test_tiered_tool_approval.py` **不需要修改**（它测试的是 Interceptor，不是 Classifier）。

但我们需要确认 `TieredToolApprovalInterceptor` 的构建位置是否受到影响。在 `_assemble_runtime()` 中，`TieredToolApprovalInterceptor` 是通过 `InterceptorChain` 传入的。检查 `core.py` 的 `_build_interceptor_chain()`：

当前 `_build_interceptor_chain()` 中没有添加 `TieredToolApprovalInterceptor`，所以无需修改。

**结论：** `tests/unit/test_tiered_tool_approval.py` 不需要修改。

- [ ] **Step 2: 运行完整 approval 相关测试套件**

```bash
pytest tests/unit/agents/react/test_approval.py tests/unit/test_argument_matcher.py tests/unit/approval/ -v
```

Expected: all tests pass.

- [ ] **Step 3: Commit**

```bash
git commit --allow-empty -m "test(approval): verify all approval tests pass after refactor"
```

---

## Task 7: 端到端验证

**Files:**
- 无需修改文件，只运行验证

- [ ] **Step 1: 运行框架 lint**

```bash
cd F:/tool/pythonProject/ModexAgent
ruff check framework/approval/config.py framework/agents/react/approval.py framework/interceptor/builtin/tool_approval.py framework/agents/react/assembler.py
```

Expected: no errors.

- [ ] **Step 2: 运行框架类型检查**

```bash
mypy framework/approval/config.py framework/agents/react/approval.py framework/interceptor/builtin/tool_approval.py
```

Expected: no type errors.

- [ ] **Step 3: 运行完整单元测试**

```bash
pytest tests/unit/ -v --tb=short
```

Expected: all tests pass.

- [ ] **Step 4: Commit**

```bash
git commit --allow-empty -m "chore: lint and type check pass"
```

---

## Self-Review Checklist

### Spec coverage

| 设计文档要求 | 对应 Task |
|-------------|----------|
| Agent 级审批配置（AgentApprovalConfig） | Task 1 |
| Tool 级路径配置（ToolApprovalConfig） | Task 1 |
| TieredToolApprovalClassifier 改造 | Task 3 |
| ArgumentMatcher 改造（project_root, fnmatch） | Task 2 |
| RuntimeServicesConfig 新增 project_root | Task 4 |
| 示例项目 _assemble_runtime() 改造 | Task 5 |
| bot_config.yml 新增 agent.approval | Task 5 |
| 删除 dangerous_tools 硬编码 | Task 5 |
| 跨平台路径解析 | Task 2 |

### Placeholder scan

- 无 TBD、TODO、"implement later"
- 所有代码块包含完整实现
- 所有测试包含断言

### Type consistency

- `AgentApprovalConfig` / `ToolApprovalConfig` 定义在 Task 1，在 Task 3 和 Task 5 中使用，字段名一致
- `ArgumentMatcher.__init__(project_root=...)` 定义在 Task 2，在 Task 3 和 Task 5 中调用，签名一致
- `TieredToolApprovalClassifier.__init__(config=..., argument_matcher=...)` 定义在 Task 3，在 Task 5 中调用，签名一致
