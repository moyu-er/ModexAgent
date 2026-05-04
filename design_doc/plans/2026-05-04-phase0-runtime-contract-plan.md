# Phase 0: Runtime Contract + 文档同步 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Write `runtime-contract-design.md` (target architecture contract) + fix stale paths/typos in CLAUDE.md, AGENTS.md, pyproject.toml, recommend.md.

**Architecture:** Two independent PRs: PR #1 = contract + 5 doc fixes (pure documentation), PR #2 = MessageRole 双定义合并 (1 行代码改动 + import fix).

**Tech Stack:** Markdown, TOML, Python (only PR #2)

---

## PR #1: Runtime Contract + Doc Fixes (0.1–0.5)

### Task 1: Fix CLAUDE.md stale paths (0.2)

**Files:**
- Modify: `CLAUDE.md:101,103,160,171`

- [ ] **Step 1: Fix line 101 — `agent_docs/` reference**

Edit `CLAUDE.md`, replace line 101:
```
├── memory/                # Three-layer memory system + redesign docs in agent_docs/
```
with:
```
├── memory/                # Three-layer memory system + redesign docs in design_doc/
```

- [ ] **Step 2: Fix line 102–103 — `managers/` → `layers/` + rename**

Replace lines 102–103:
```
│   ├── managers/          # ShortTerm, History, LongTerm layer managers
```
with:
```
│   ├── layers/            # Session, Archive, Knowledge layer managers
```

- [ ] **Step 3: Fix line 160 — Memory system layer names**

Replace line 160:
```
Three-layer with scope isolation: `Short-term → History → Long-term (SOUL/USER/MEMORY.md)`.
```
with:
```
Three-layer with scope isolation: `Session (short-term) → Archive (history) → Knowledge (long-term, SOUL/USER/MEMORY.md)`.
```

- [ ] **Step 4: Fix line 171 — `agent_docs/` reference**

Replace line 171:
```
**Design documents:** `agent_docs/memory-system-redesign.md` and `agent_docs/memory-system-redesign-plan.md` describe the ongoing P0-P5 redesign.
```
with:
```
**Design documents:** `design_doc/` contains the latest memory system design docs; `docs/memory-system.md` describes the current architecture.
```

- [ ] **Step 5: Verify with grep**

Run: `grep -n "managers/\|agent_docs/" CLAUDE.md`
Expected: 0 hits

- [ ] **Step 6: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: fix CLAUDE.md stale paths — managers/ → layers/, agent_docs/ → design_doc/"
```

---

### Task 2: Fix AGENTS.md numbering bug + stale path (0.3)

**Files:**
- Modify: `AGENTS.md:16,89-90`

- [ ] **Step 1: Fix line 16 — `agent_docs/` reference**

Replace line 16:
```
Tests are under `tests/unit/`, `tests/integration/`, and `tests/e2e/`. Documentation in `docs/`, examples in `examples/`, design docs in `agent_docs/`.
```
with:
```
Tests are under `tests/unit/`, `tests/integration/`, and `tests/e2e/`. Documentation in `docs/`, examples in `examples/`, design docs in `design_doc/`.
```

- [ ] **Step 2: Fix lines 89–90 — 编号 4-4 duplicate**

Replace lines 89–90:
```
4. 类设计必须做好抽象化, 禁止直接使用具体实现类, 而是应该使用抽象类或接口, 便于扩展/拔插/自定义
4. framework目录中的都是框架代码, examples中的都是业务代码使用示例, 必须按照业务需求和框架设计/通用性区分修改内容, 例如不能将业务代码中的配置写死到框架中
```
with:
```
4. 类设计必须做好抽象化, 禁止直接使用具体实现类, 而是应该使用抽象类或接口, 便于扩展/拔插/自定义
5. framework目录中的都是框架代码, examples中的都是业务代码使用示例, 必须按照业务需求和框架设计/通用性区分修改内容, 例如不能将业务代码中的配置写死到框架中
```

- [ ] **Step 3: Verify with grep**

Run: `grep -n "agent_docs/" AGENTS.md`
Expected: 0 hits

- [ ] **Step 4: Commit**

```bash
git add AGENTS.md
git commit -m "docs: fix AGENTS.md — agent_docs/ → design_doc/, 修复编号 4-4 重复 bug"
```

---

### Task 3: Fix pyproject.toml Python version unify (0.4)

**Files:**
- Modify: `pyproject.toml:24,86,93`

- [ ] **Step 1: Remove Python 3.11 classifier (line 24)**

Delete line 24:
```
    "Programming Language :: Python :: 3.11",
```

- [ ] **Step 2: Fix ruff target-version (line 86)**

Replace line 86:
```
target-version = "py311"
```
with:
```
target-version = "py312"
```

- [ ] **Step 3: Fix mypy python_version (line 93)**

Replace line 93:
```
python_version = "3.11"
```
with:
```
python_version = "3.12"
```

- [ ] **Step 4: Verify**

Run:
```bash
ruff check --show-settings framework/core/types.py 2>&1 | grep target-version
```
Expected: `target-version = Py312`

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml
git commit -m "chore: unify pyproject.toml to Python 3.12 (ruff + mypy + classifier)"
```

---

### Task 4: Add recommend.md status audit table (0.5)

**Files:**
- Modify: `design_doc/recommend.md` (append at end)

- [ ] **Step 1: Read current end of recommend.md to confirm**

Already done — file ends at line 987.

- [ ] **Step 2: Append status audit section**

Append the following after line 987:

```markdown

---

# 15. Status Audit (2026-05-04)

> 以下为对本建议文档的状态审计。仅标注各条建议的当前落地状态及对应 git commit, **不修改原文任何内容**。

| recommend.md 主张 | 当前状态 | 证据 / commit |
|---|---|---|
| §1 Pipeline 不拥有 turn 语义 | ✅ 已基本落地 | ReActAgent 拥有 turn/iteration; `pipeline.py` 仍待拆类 (Phase 5) |
| §2.1 Hook/Interceptor/Control/Approval 职责边界 | ✅ 已落地 | 四层独立目录 + ABC |
| §2.2 Approval = policy + suspend/resume + runtime state + control command | ✅ 已落地 | `22f1749` ApprovalClassifier + ApprovalRuntime; `6df89a6` 移除 ReActRuntime.suspend_strategy |
| §2.3 clean / full 模式 | ✅ 已落地 | `825896f` ReActRuntime; `runtime.py:42-127` |
| §3.1 MemorySystem 唯一入口 | ✅ 已落地 | `MemorySystemContextManager` 存在且被广泛引用 |
| §3.2 WorkingMemory 移除 | ✅ 已清空 | 全仓 grep 仅本文命中, 代码 0 残留 |
| §3.3 Memory 三层重命名 Session/Archive/Knowledge | ✅ 已落地 | `framework/memory/layers/{session,archive,knowledge}.py` |
| §3.4 Memory ≠ RuntimeStateStore | ✅ 已落地 | `e7fb8b3` 移除 checkpoint alias 冗余 |
| §3.5 MemoryProvider 降级为扩展 | ℹ️ 维持现状 | `MemoryProvider` 已是可选扩展 (prefetch/search), 非 canonical 写入入口 |
| §5.1 ToolManager 不负责审批 | ✅ 已落地 | `3185df6` 删除 ToolPolicyGuardHook |
| §6.1 supports_streaming → StreamingMode | ⚠️ 半迁移 | `StreamingMode` 枚举已存在; `pipeline/adapters.py` 仍有 7 处 `supports_streaming` — Phase 1 收尾 |
| §6.2 Memory adapter → compat/ | ❌ 取消 | 代码中无旧 adapter 需要 compat, 此建议不再执行 |
| §7.2 bot_project 默认 pipeline | ⏹ 待 Phase 1 | 当前 `bot_service.py:89` 写死 `pool` |
| §8 Governance / Safety / Interceptor 拆分 | ⏹ 待评估 | 未在本次 Phase 0-5 路线图中排入 |
| Phase 4 minimal examples | ⏹ 待 Phase 4 | `examples/` 仅 `bot_project/`, `sandbox/`, `security/` |
| TURN/ITERATION interceptor inert | ✅ 已过时 | `dc49b80` / `6832fc0` 已接入 |
| Control drain 5 安全边界 | ✅ 已落地 | `f20a15f` |
| Pipeline decomposition | ⏳ 部分 | `a173bca` 提取了 6 个私有方法, 未拆类 — Phase 5 |
```

- [ ] **Step 3: Commit**

```bash
git add design_doc/recommend.md
git commit -m "docs: add status audit table to recommend.md (2026-05-04 cross-check)"
```

---

### Task 5: Final verify + create PR #1

- [ ] **Step 1: Run all verification greps**

```bash
# 0 遗漏的 managers/ 路径
grep -rn "managers/" CLAUDE.md AGENTS.md
# Expected: 0

# 0 遗漏的 agent_docs/ 路径
grep -rn "agent_docs/" CLAUDE.md AGENTS.md
# Expected: 0

# pyproject target version
grep "target-version\|python_version" pyproject.toml
# Expected: both = "py312" / "3.12"
```

- [ ] **Step 2: Confirm spec file exists and is committed**

Run:
```bash
git log --oneline -5
```

- [ ] **Step 3: Create PR #1**

```bash
git push origin develop_gyt
gh pr create --title "docs: Phase 0 — runtime contract + doc sync" --body "$(cat <<'EOF'
## Summary
- 新增 `design_doc/2026-05-04-runtime-contract-design.md` (target architecture contract)
- 修复 CLAUDE.md / AGENTS.md 过时路径 (managers/ → layers/, agent_docs/ → design_doc/)
- 修复 AGENTS.md 编号 4-4 重复 bug
- 统一 pyproject.toml 到 Python 3.12
- recommend.md 追加状态审计表

## Test plan
- [ ] grep -rn "managers/\|agent_docs/" CLAUDE.md AGENTS.md → 0 命中
- [ ] ruff check framework/ → 不变
- [ ] mypy framework/ → baseline 不变

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

---

## PR #2: MessageRole 双定义合并 (0.6)

### Task 6: Delete legacy MessageRole + fix single caller

**Files:**
- Modify: `framework/core/constants.py:9-15` (delete class)
- Modify: `framework/core/tool_manager.py:260` (fix import)

**Background:**
- Canonical: `framework/core/types.py:27` → `class MessageRole(StrEnum)` with `SYSTEM/USER/ASSISTANT/TOOL/AGENT`
- Legacy: `framework/core/constants.py:9` → `class MessageRole(str, Enum)` with `SYSTEM/USER/ASSISTANT/TOOL/FUNCTION`
- `MessageRole.FUNCTION` has 0 callers in the entire repo
- `MessageRole.AGENT` (only in types.py) has 3 callers, all importing from `types.py`
- Only 1 file imports from constants: `tool_manager.py:260`

- [ ] **Step 1: Fix `tool_manager.py:260` import**

Replace line 260:
```python
        from .constants import MessageRole
```
with:
```python
        from .types import MessageRole
```

- [ ] **Step 2: Delete `MessageRole` class from `constants.py`**

Delete lines 9–15 from `framework/core/constants.py`:
```python
class MessageRole(str, Enum):
    """消息角色"""
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"
    FUNCTION = "function"
```

- [ ] **Step 3: Remove unused `Enum` import if no longer needed**

Check if `constants.py` still uses `Enum` after deletion. It does — `ToolCallType(str, Enum)`, `ToolChoice(str, Enum)`, `FinishReason(str, Enum)` all use it. No change needed.

- [ ] **Step 4: Run tests**

```bash
pytest tests/unit/ -q --tb=line
```
Expected: all pass (same baseline)

- [ ] **Step 5: Run mypy**

```bash
mypy framework/core/constants.py framework/core/tool_manager.py framework/core/types.py
```
Expected: no new errors

- [ ] **Step 6: Verify no stale import remains**

```bash
grep -rn "from.*constants.*import.*MessageRole" framework/ examples/ tests/
```
Expected: 0 hits

- [ ] **Step 7: Commit**

```bash
git add framework/core/constants.py framework/core/tool_manager.py
git commit -m "refactor: merge MessageRole — delete legacy constants.py version, keep types.py canonical"
```

---

### Task 7: Create PR #2

- [ ] **Step 1: Push and create PR**

```bash
git push origin develop_gyt
gh pr create --title "refactor: merge MessageRole double definition" --body "$(cat <<'EOF'
## Summary
- 删除 `framework/core/constants.py` 中的 legacy `MessageRole(str, Enum)` 定义
- `framework/core/tool_manager.py:260` import 改为从 `core.types` (唯一 canonical 版本)
- `MessageRole.FUNCTION` 全仓 0 引用, 安全删除

## Test plan
- [ ] `pytest tests/unit/ -q --tb=line` → 全绿
- [ ] `grep -rn "from.*constants.*import.*MessageRole" framework/` → 0 命中
- [ ] `mypy framework/core/` → baseline 不变

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

---

## Completion Check

- [ ] PR #1 merged: contract + 5 doc fixes
- [ ] PR #2 merged: MessageRole 合并
- [ ] `grep -rn "managers/\|agent_docs/" CLAUDE.md AGENTS.md` → 0
- [ ] `grep -rn "from.*constants.*import.*MessageRole" framework/` → 0
- [ ] `pytest tests/unit/ -q --tb=line` 全绿 (both PRs)
- [ ] `ruff check framework/` clean
- [ ] Phase 0 对齐表 (spec Section 4) row 0 → `✅`
