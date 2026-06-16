# Workspace 统一管理器设计

- 日期：2026-06-16
- 范围：`examples/bot_project` 业务层 + `framework/workspace/` 框架层
- 状态：设计已确认，待实施

## 1. 背景与动机

当前 workspace 切换机制存在三类问题：

1. **切换实现散乱**。切换逻辑分散在 `bot/service/core.py` 的 7 个 `_rebuild_*` 方法 + `_on_ws_*` 回调 + `_ws_*` 路径 helper + `WebUIService.update_session_stores` 覆写中，且大量依赖对私有属性的直接赋值（`policy._pruned_manager`、`interceptor.handler._store`、`factory._trace_store._base_dir`、`inbox_server._tracker._workspace`、`factory._default_turn_store`、`svc._memory_dir/_runtime_dir/_pruned_manager`）。
2. **全局可变状态**。切换依赖 `os.chdir()`（进程全局 CWD）+ busy-check（agent 正忙时拒绝切换）。这决定了进程同一时刻只能有一个活跃 workspace。
3. **路径无隔离保证**。数据路径由各组件自行拼接，无统一净化，理论上可逃逸 workspace 根目录。

本轮整改目标（**G1，本轮交付**）：

- 收敛 1-16 本地文件数据源到一个统一抽象，路径全部为 workspace 根的相对子路径、禁止 `..`、硬保证 containment。
- 用 `WorkspaceManager` + `Workspace` 统一切换，废掉 `os.chdir` 与散乱重建。
- **去掉 busy-check**：在途 turn 持数据快照、不受切换影响。
- 读写数据源统一收敛（含 summarizer/consolidator/reviewer 写入路径）。
- 为未来多 workspace 并发 / 多进程留扩展位，但**不增加本轮工作量**。

明确**不做**（G2，留作后续）：

- 进程内多 workspace 并发、多前端各自 workspace 路由。
- 数据迁移与向后兼容——历史错误实现完整清理。

## 2. 资源归属边界（核心判据）

**判据：一个资源读写的文件是否落在该 workspace 的数据根目录下。**

- 是 → workspace 级或 per-pool 数据源，进 `Workspace`。
- 否（读项目/配置文件、持有网络连接、外部服务）→ 部署级资源，**不进 Workspace、切换不动、不复制**。

| 资源 | 归属 | 持有者 |
|---|---|---|
| memory（含 pruned、fork_contexts） | per-pool 数据 | `Workspace.pool_data[pool]` |
| turn_store / command_store / trace / output | per-pool 数据 | `Workspace.pool_data[pool]` |
| experience（EXPERIENCE.md + meta） | per-pool 数据 | `Workspace.pool_data[pool]` |
| terminal_manager | per-pool 运行态 | `PoolInstance`（shell cwd 绑定 workspace，切换时关闭，不可 rebase） |
| inbox + 去重 tracker | workspace 级 | `Workspace` |
| pool_sessions 路由 | workspace 级 | `Workspace` |
| transcript（sessions） | workspace 级 | `Workspace` |
| session_index（SessionInfo） | workspace 级 | `Workspace` |
| overflow | workspace 级 | `Workspace` |
| MCP / LLM provider / 工具管理器 / skill / agent 描述符 / input adapter | 部署级 | `BotService` / `PoolInstance` |

## 3. 对象模型

### 3.1 三角色

```
WorkspaceManager（部署级单例）
  · 校验路径 → 创建/取回 Workspace
  · 持有「活跃 Workspace」（今天 1 个；将来 dict[连接, Workspace]）
  · 统一 switch() / exit() / restore()
  · turn 解析入口 resolve_workspace() → 活跃 Workspace
        │ 拥有 0..N（今天活跃 1 个）
        ▼
Workspace（一个 workspace 隔离作用域，构造后不可变）
  · root：已校验绝对路径；所有子路径相对它、禁止 ..
  · workspace 级 store 对象
  · pool_data: dict[pool_name, PoolData]
  · workspace 级后台任务引用（dream/curator）
        │ 提供路径与 PoolData
        ▼
PoolInstance（部署级共享，workspace 无关）
  · provider / tool_manager / skill / MCP / terminal_manager / descriptors
  · turn 开始时从活跃 Workspace 取 pool_data 快照使用
```

`Workspace` 池无关，`PoolInstance` workspace 无关，两者靠「路径派生 + 数据快照」连接。

### 3.2 `Workspace`

构造后不可变。持有：

- `root: Path`（已校验绝对路径）
- workspace 级 store 对象：`inbox_server` + tracker、`pool_session_store`、`transcript_store`、`session_index_store`、`overflow_store`
- `pool_data: dict[str, PoolData]`
- 后台任务对象：`dream_engine`（基于本 Workspace 默认 pool 的 memory）+ 各 pool 的 `curator`（基于本 Workspace 的 per-pool experience）。**随 Workspace 构建与启停**——切换时旧 Workspace 的任务停止、新 Workspace 的任务启动。

> dream/curator 的数据目标始终是「自身 Workspace」的数据，因此它们随 Workspace 而非随 `BotService` 存在。switch 序列（§6）步骤 4 停旧、步骤 8 起新即指此。

暴露类型化路径访问器（全项目唯一定义布局，见 §4）。

### 3.3 `PoolData`（冻结 bundle，一个 pool × 一个 workspace）

- `memory_system`（含 pruned_manager、archive/knowledge manager、summarizer、consolidator）
- `turn_store` / `command_store` / `trace_store`
- `experience`：注入 manager + experience 目录 + meta_store
- `comm_paths`：`memory_dir` / `runtime_dir`（供 `AgentCommunicationService`）

### 3.4 `PoolInstance`（瘦身后）

仅持有部署级资源：provider、tool_manager、skill_manager、MCP、terminal_manager、descriptors、broker bridge。数据访问改为 turn 开始时 resolve。

## 4. 路径布局与校验

### 4.1 Root 语义

- `WorkspaceManager.switch(target)` → `root = target / data_dir_name`。
- `data_dir_name` 由配置提供，默认 `.modex`（**不写死**，替换现有环境变量直读点）。
- workspace = 一个真实工作目录，bot 数据与其下 `.modex/` 同居——与现状语义一致。
- `cwd.json`（记忆上次 target）仍在 **home**（启动目录）的 `<data_dir_name>/cwd.json`，不在任何 Workspace root 内。`restore()` 读它切换。

### 4.2 布局 schema（Workspace 内唯一定义）

```
<root = target/<data_dir_name>>/
├── memory/<pool>/                  # MemorySystem（session/archive/knowledge）
│   ├── pruned/                     # 修剪目录
│   └── fork_contexts/              # subagent fork XML
├── runtime_state/<pool>/
│   ├── turns/                      # JsonFileTurnStateStore
│   ├── commands/                   # JsonFileRuntimeCommandStore
│   ├── trace/                      # JsonFileTraceStore
│   └── output/<session>/OUTPUT.md  # subagent 产出
├── experiences/<pool>/<agent>/     # 经验学习
├── inbox/                          # 【workspace 级】InboxServer + 去重 tracker
├── pool_sessions/                  # 【workspace 级】PoolSessionStore
├── sessions/                       # 【workspace 级】transcript（WebUI；不用则空）
├── session_index/                  # 【workspace 级】SessionInfo（LocalFileSessionStore）
└── overflow/                       # 【workspace 级】工具结果溢出
```

消灭 `BotService._ws_*`、`pool_builder` 内联拼接、`WebUIService._sessions_dir/_session_index_dir`、overflow 散在 root 的定义。

### 4.3 校验：两层 + 一个净化器

**第一层——root 合法性**（`switch` 时，失败返回 `CdResult(error=...)`，不切）：

1. `target` 解析后必须绝对路径。
2. `target` 存在且是目录。
3. `root = target/<data_dir_name>` 可创建、可写。
4. 幂等：`target` 已是当前活跃则直接成功。

**第二层——子路径 containment（隔离硬保证）**：

所有 Workspace 访问器不直接拼接用户/配置字符串，经过统一净化器 `_safe_segment(name)`：

- 替换所有路径分隔符、`..`、空白控制符等（收敛现有 `safe_filename`（session_store）与 `_safe_segment`（turn_store）两份实现成一份）。
- 拼出候选路径后 `resolve()`，断言 `resolved.is_relative_to(root_resolved)`；任何逃逸直接抛错、不写入。

可变段（`pool_name`、`agent_name`、`session_id`）全部走净化器；固定段是常量。净化是防御性硬保证，不依赖配置值可信。

### 4.4 mkdir 语义

- Workspace 创建时：mkdir workspace 级骨架（`inbox`/`pool_sessions`/`sessions`/`session_index`/`overflow`）。
- per-pool 目录：由各 store 自己 `initialize()` 时 `mkdir(parents=True, exist_ok=True)`（现状如此，不改）。Workspace 只给路径，不抢 mkdir 职责。

## 5. turn 快照契约（免 busy-check 的核心）

### 5.1 契约

- **turn 开始**：runner 执行 `ws = workspace_manager.resolve_workspace()`（今天 = 返回活跃 Workspace），再 `data = ws.pool_data[pool_name]`，把 `ws`/`data` 线程化进 `AgentContext` / turn 执行。
- **turn 全程**：记忆、turn_store、trace、experience、overflow、pruned 的读与写一律走快照 `data`，**禁止**中途再读 `context_manager.memory_system` / `pipeline.turn_store` 等可变属性。
- **切换中途**：`switch()` 把 `manager._active` 换成新 Workspace（新 pool_data），停掉旧 Workspace 后台任务。在途 turn 持有旧 `ws` 引用（GC 保活到 turn 结束），不受影响；新 turn 用新 Workspace。

### 5.2 写入路径统一收敛

| 写入组件 | 触发时机 | 数据源 | 处理 |
|---|---|---|---|
| session 记忆追加/保存 | turn 内 | 快照 `memory_system` | 随快照 |
| ArchiveSummarizer（归档） | turn 内 cleanup_session | 快照 `memory_system.archive_manager` | 归 MemorySystem 所有，随快照自动正确 |
| KnowledgeConsolidator | turn 内归档 | 快照 `memory_system.knowledge_manager` | 归 MemorySystem 所有，随快照自动正确 |
| PrunedManager | turn 内 | 快照 `memory_system.pruned_manager` | 随快照 |
| ExperienceReviewHook/Agent | turn 后 hook | 快照 experience 目录 + meta | **需改**：不再读可变 `dir_ref`，改读 turn 快照路径 |
| Overflow handler | turn 内（工具结果） | 快照 `overflow_store` | 随 Workspace 快照 |
| trace | turn 内 | 快照 `trace_store` | 随快照 |
| DreamEngine | 后台 loop | 自身 Workspace 的默认 pool memory | **需改**：从 `BotService` 全局移到 Workspace 级（随 Workspace 构建启停） |
| ExperienceCurator | 后台 loop | 自身 Workspace 的 per-pool experience | **需改**：随 Workspace 构建启停（不再 per-pool 挂在 PoolInstance 由 BotService 起） |

规则：turn 内写入（含 summarizer/consolidator/reviewer/pruned/overflow/trace）绑死 turn 快照；后台写入（dream/curator）绑活跃 Workspace。切换中途不串数据。

## 6. switch 序列（收敛进 WorkspaceManager）

全部在 manager 锁下执行，替代散落的 `_rebuild_*`：

1. 校验 `target` → 算 `root = target / data_dir_name` → 不合法返回 `CdResult(error=...)`，不切。
2. 幂等：`target` 已是活跃 → 直接成功。
3. **构建新 Workspace**：mkdir 骨架 → 建 workspace 级 store → 为每个配置的 pool 建 `PoolData`（close 旧 memory、`create_memory(新路径)`、init、sync pruned、long-term defaults、新 turn/cmd/trace store、experience refs、comm paths）→ 建本 Workspace 的 `dream_engine`（默认 pool memory）+ 各 pool `curator`。
4. **停旧后台任务**（dream/curator，引用旧 Workspace 数据）。
5. **关终端**：遍历各 pool `terminal_manager` 关闭所有会话。
6. **清 subagent 缓存**（持有旧 Workspace 引用）。
7. **激活**：`self._active = new_workspace`；PoolInstance「当前数据」指针拨到新 Workspace。
8. **启新后台任务**：dream/curator 指向新 Workspace。
9. **持久化**：写或清 home 的 `cwd.json`。

### 6.1 原子性

- 步骤 3 构建期任一失败 → **不激活、不动 `_active`、不写 cwd.json**，返回 `CALLBACK_ERROR`/`PERMISSION_DENIED`。旧活跃 Workspace 完好，进程继续用旧的。
- 走到步骤 7（激活）后，后续失败（启任务 / 写 cwd.json）仅记日志，新 Workspace 已生效。

## 7. 组件集成

### 7.1 `BotService`

**删除**：`_ws_*` helper、`_on_ws_stop_and_rebuild`、`_on_ws_terminal_reset`、`_rebuild_pool_memory`、`_rebuild_memory_for_target`、`_rebuild_experience`、`_rebuild_shared_infrastructure`、`_rebuild_session_stores`、`_update_communication_paths`、`_rebuild_overflow_store`、`update_session_stores`（基类）、`workspace_context`（框架 os.chdir 版）、全部私有属性直改。

**保留/新增**：`self.workspace_manager: WorkspaceManager`。`initialize()`：建 manager → `restore()` → 建 Workspace（含各 pool 的 PoolData）→ 建 PoolInstance（仅部署级资源）。cd/exit handler 指向 `manager.switch()`/`manager.exit()`。dream/curator 后台任务由 manager 在 Workspace 激活时启停。

### 7.2 `pool_builder` / `PoolInstance`

- `create_pool` 只建部署级资源（provider、tool_manager、skill、MCP、terminal_manager、descriptors、broker bridge）。
- per-pool 数据由 `Workspace.build_pool_data(pool_name)` 构建，存入 `Workspace.pool_data[pool_name]`。
- turn 执行入口改为 resolve 快照。

### 7.3 `WebUIService`

- 删 `update_session_stores` 覆写、`_sessions_dir`/`_session_index_dir`、自管 `_transcript_store`/`_session_store`。
- transcript / session_index 从活跃 Workspace 取。启动时按当前活跃 Workspace rebase（覆盖初始 restore 场景）。

### 7.4 写入路径收敛

- summarizer/consolidator：无需改代码，确保 `cleanup_session`/归档走快照 `memory_system`。
- ExperienceReviewHook：构造时不再收 `dir_ref` 闭包；改从 turn 上下文取快照 experience 路径。
- DreamEngine：从 `BotService` 字段移到 `Workspace`。
- ExperienceCurator：从 `PoolInstance` + `BotService` 启停移到 `Workspace`（随 Workspace 构建启停，目标为本 Workspace 的 per-pool experience）。

### 7.5 框架层 `framework/workspace/`

- **退役** `DefaultWorkspaceContext`（os.chdir 引擎）与 `WorkspaceSwitchCallback` 广播机制。
- **保留** `parse_user_path`、`CdResult`/`CdError`、`Cd/Exit/Pwd` 命令 handler（改为调用注入的 switch callable，不再绑定具体 WorkspaceContext 实现）。

### 7.6 私有属性直改 → 正式归属/rebase

| 现状直改 | 新模型 |
|---|---|
| `policy._pruned_manager = ...` | PoolData 拥有 pruned；注入策略从 PoolData 取 |
| `interceptor.handler._store = ...` | overflow_store 归 Workspace，handler 从 Workspace 取 |
| `factory._trace_store._base_dir = ...` | trace_store 归 PoolData（随 Workspace 重建，无需改 base） |
| `factory._default_turn_store = ...` | turn_store 归 PoolData |
| `inbox_server._tracker._workspace = ...` | inbox 归 Workspace，整体随 Workspace 切换 |
| `svc._memory_dir/_runtime_dir/_pruned_manager = ...` | comm paths 收进 PoolData |

## 8. 错误处理（汇总）

- switch 构建期失败 → 不激活、`_active` 不动、不写 cwd.json、返回带 `CdError` 的 `CdResult`；旧活跃 Workspace 完好。
- switch 激活后失败 → 仅记日志，新 Workspace 已生效。
- 路径逃逸 → 净化器 + `is_relative_to` 校验，逃逸即抛。
- restore → cwd.json 路径失效则跳过、留在 home。

## 9. 测试策略

- **路径层**：每个访问器返回路径 `is_relative_to(root)`；注入恶意 pool/agent 名（`../`、绝对路径、符号链接）断言被净化、不逃逸。
- **switch 原子性**：构造期注入失败 → `_active` 未变、旧数据可继续读写、cwd.json 未写。
- **快照隔离（核心）**：起 turn A（workspace A）→ 切到 B → A 的写入（记忆/归档/经验/trace/overflow）仍落 A、不落 B；B 的后台任务只动 B。
- **写入收敛**：summarizer/consolidator 产物落在 turn 快照 memory_system；ExperienceReviewHook 写入落在快照 experience dir（turn 进行中切换，断言经验写到 turn 起始 workspace）。
- **回归**：现有 workspace 切换相关测试改为驱动 `WorkspaceManager.switch()`；mock 不编码 os.chdir 或私有属性等旧实现细节。

## 10. 清理范围（不迁移、不兼容，完整删除）

- 删 `framework/workspace/context.py` 的 `DefaultWorkspaceContext` + `WorkspaceSwitchCallback` 广播。
- 删 `BotService` 全部 `_rebuild_*` / `_ws_*` / `_on_ws_*` / `update_session_stores` 及私有属性直改。
- 删 `WebUIService` 的 `update_session_stores`、自管路径 helper。
- per-pool 数据从 `PoolInstance` 字段迁出到 `Workspace.pool_data`。
- overflow 从「散在 root」移到 `overflow/`（旧 root 下 overflow 文件忽略不迁）。
- `.modex` 由配置提供（默认值），替换环境变量直读写死点。

## 11. 扩展位（本轮不实现，仅保证不返工）

三处为零额外成本的扩展钩子：

1. **废 `os.chdir`** —— 本轮清理的必经步骤，顺带使未来多 workspace 无害。
2. **turn 快照 Workspace** —— 本轮即去掉 busy-check，天然并发就绪。
3. **`resolve_workspace()` 中间层** —— 今天返回活跃 Workspace，将来换「按连接查表」是换实现不改调用方。

未来两条路（任选，均以本轮成果为内核）：

- **进程内并发**：`Manager` 持 `dict[连接, Workspace]`，`resolve_workspace(ctx)` 按连接查；`WorkspaceManager` 生命周期扩 create/evict。
- **多进程**：每 workspace/前端一个进程，每进程单一 Workspace（本轮模型），「多」靠部署多进程达成。隔离已在文件系统层，无需进程内并发工作。

## 12. 不做的事

- 不做进程内多 workspace 并发 / 多前端路由（G2）。
- 不做数据迁移与向后兼容——历史错误实现完整清理。
- 不重构定时任务调度结构——dream/curator 仅改为指向活跃 Workspace 数据。
