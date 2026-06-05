# /cd /exit /pwd — 工作空间切换设计

## 目标

运行时切换工作目录和数据体系。`/cd <path>` 切换，`/exit` 恢复，`/pwd` 查看。

## 核心约束

1. **不修改 Pipeline 内部流程** — 只新增 `has_active_sessions()` 方法
2. **不修改 Scope 系统** — 记忆隔离通过物理路径实现，scope key 不变
3. **不修改工具代码** — `os.chdir()` 统一切换
4. **框架层无感知** — `WorkspaceContext` 是框架接口，所有存储对象（MemorySystem / InboxServer / TurnStateStore / OverflowStore）只知道自己的 `data_dir`，不感知切换
5. **业务层统一配置** — BotService 通过 `_ws_memory/_ws_runtime/_ws_approval/_ws_inbox` 统一路径，init 和 callback 同源
6. **Adapter 层拦截** — cd/exit 在 `_try_intercept_control()` 被拦截，不进入 pipeline/pool dispatch，避免 self-blocking
7. **Inex** — 跟在 cd 后切换，exit 后恢复

## 切换模型

```
cd 前:  所有存储对象 → home/.modex/
cd 后:  所有存储对象 → target/.modex/ （同一套对象，路径变了）
exit 后: 所有存储对象 → home/.modex/ （恢复）
```

框架层每个存储对象只接受 `data_dir`，不知道"切换"：

| 对象 | 切换方式 |
|---|---|
| `DefaultMemorySystem` | 关旧 → `create_memory(config, provider, new_dir)` → 换引用 |
| `LocalFileInboxServer` | 更新 `._workspace` 属性 |
| `JsonFileTurnStateStore` | 新实例 `(new_dir/turns, codec)` → 换引用 |
| `JsonFileRuntimeCommandStore` | 新实例 `(new_dir/commands)` → 换引用 |
| `LocalFileToolOverflowStore` | 新实例 `(workspace=new_dir)` → 换引用 + 更新 interceptor 链 |
| `approval_workspace` | 更新 `_approval_workspace` + pipeline 引用 |

## 运行安全

**两层保护，互补而非冗余：**

| 层 | 位置 | 作用 |
|---|---|---|
| Adapter 拦截 | `adapters.py:_try_intercept_control` | cd/exit 不进入 pool dispatch，不自增 `_active_session_counts` |
| active_checker | `context.py:_switch` | 切换前校验无 agent 运行 |

**Pipeline 模式**: `Pipeline.has_active_sessions()` — `any(not task.done() for task in _session_tasks.values())`。
Task 在 `_run_agent_turn` 加入，`finally` 移除。Subagent 在父 session 的 task 中，已覆盖。

**Pool 模式**: `AgentPool._active_session_counts[agent] > 0`。
dispatch 时 +1，finally 时 -1。cd/exit 被 adapter 拦截不进入 dispatch，不会自阻塞。Subagent 通过同一 pool dispatch，已覆盖。

## 目录布局

```python
# core.py — 统一入口（init 和 callback 同一来源）
@staticmethod
def _ws_memory(data_dir: Path) -> Path:   # → .modex/memory
def _ws_runtime(data_dir: Path) -> Path:  # → .modex/runtime_state
def _ws_approval(data_dir: Path) -> Path: # → .modex/approval
def _ws_inbox(data_dir: Path) -> Path:    # → .modex/inbox
```

```
.modex/
├── cwd.json           ← 当前工作路径持久化
├── memory/            ← 记忆体系（knowledge + archive + session）
│   └── {pool_name}/
├── runtime_state/     ← TurnState + RuntimeCommand
│   └── {pool_name}/
│       ├── turns/
│       └── commands/
├── approval/          ← 审批状态
├── inbox/             ← 消息入口
└── pool_sessions/     ← session→pool 路由
```

## 回调设计

**3 个回调，按注册顺序执行：**

| ① | `_BackgroundStop` | cancel dream_task + clear injection_queues |
| ② | `_MemoryRebuild` | 重建全部存储对象（memory/inbox/runtime/approval/overflow）+ plugin 注入 + knowledge 初始化 |
| ③ | `_TerminalReset` | close 全部终端会话 |

Pool 模式不重建 pool 实例——只更新每个 pool 内部存储对象的路径/引用。

## 架构

```
framework/workspace/
  models.py        CdResult, WorkspaceSwitchCallback
  parse.py         parse_user_path（pathlib + \→/ 归一化）
  context.py       WorkspaceContext (ABC) + DefaultWorkspaceContext
  handlers.py      CdCommandHandler, ExitCommandHandler, PwdCommandHandler

framework/commands/
  constants.py     BuiltinCommand: CD / EXIT / PWD

framework/pipeline/
  pipeline.py      has_active_sessions()
  adapters.py      _try_intercept_control 拦截 cd/exit

examples/bot_project/bot/service/
  core.py          _ws_* 路径入口 + 3 回调 + active_checker
  pool_builder.py  data_dir 必填参数
```

## WorkspaceContext 接口

```python
class CdResult:
    success: bool; current_path: Path; original_path: Path
    notice: str; error: str | None = None

class WorkspaceSwitchCallback(Protocol):
    async def on_workspace_switch(old_data_dir, new_data_dir) -> None: ...

class WorkspaceContext(ABC):
    home / current / data_dir / is_home
    register_callback / cd / exit / restore
```

## _switch() 流程

```
1. target == current → 幂等返回
2. target 不存在/不是目录 → 返回失败
3. new_data_dir 不可写 → 返回失败
4. active_checker() 返回 True → 返回失败（"agents are busy"）
5. 回调: BackgroundStop → MemoryRebuild → TerminalReset
6. os.chdir(target)
7. cwd.json 写入/清除
8. _current = target
```

## 路径解析

`parse_user_path(raw, base)` — Python 标准库 `pathlib`：
- 绝对/相对、`~` `.` `..`、`\`→`/` 归一化、尾随斜杠
- 空/空白 → `ValueError`

## 失败处理（英文消息）

| 场景 | notice |
|---|---|
| 路径不存在 | `cd: path not found: '/xxx'` |
| 不是目录 | `cd: not a directory: '/xxx'` |
| 无权限 | `cd: permission denied: '/xxx'` |
| agent 忙 | `cd: agents are busy, try again later` |
| 回调异常 | `cd: internal error, reverted` |
| 已在 home | `exit: already at home` |
| 无参数 | `cd: no path specified` |
| 路径不合法 | `cd: invalid path` |
| 成功 | `switched to: /xxx` / `returned to home: /xxx` |
| /pwd | `cwd: /xxx\nhome: /yyy` |

## Knowledge 初始化修复

`_init_long_term_defaults` 原本只检查 `long_term`（旧配置字段），但 YAML 使用 `knowledge`
（新字段）。`model_post_init` 只做 `long_term → knowledge` 迁移，不做反向迁移，所以
`long_term` 永远为 None，硬编码默认值从未被调用。

**修复：**
1. 同时检查 `knowledge`（新）和 `long_term`（旧）两种配置格式
2. `default_templates_dir` 相对于 `_project_dir` 解析为绝对路径，更新 knowledge manager 的 config，确保 `ensure_defaults` 在 CWD 改变后仍能找到模板
3. 硬编码默认值作为模板的后备

## PrunedManager 同步

`FullInjectionPolicy` 在构造时缓存了 `pruned_manager` 引用。cd 后 MemorySystem 被替换，
但 `context_manager.injection_policy._pruned_manager` 仍指向旧的 pruned_manager。

**修复：** 新增 `_update_pruned_manager()` 辅助函数，在 pipeline 和 pool 两个回调中同步
injection policy 的 `pruned_manager` 引用，确保 pruned 数据目录跟随切换。

## 已验证无问题的子系统

| 子系统 | 原因 |
|--------|------|
| Archive | 内嵌于 MemorySystem 的 layer set，随 `create_memory()` 自动创建 |
| User retention buffer | 同上，通过 `MemoryLayerFactory.single_user()` 创建 |
| Inbox | `inbox_server._workspace` 已更新 + `mkdir` |
| Runtime stores | 新实例 + pipeline 引用更新 |
| Approval | `_approval_workspace` 已更新（含 pipeline） |
| Overflow store | `_rebuild_overflow_store()` 已处理 |

## 不在范围内

- 多层 cd 栈
- 多用户并发 cd
- Skills 目录跟随切换
