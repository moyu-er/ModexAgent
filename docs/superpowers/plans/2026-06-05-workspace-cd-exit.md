# /cd /exit /pwd 实现计划

**Goal:** 运行时切换工作目录和数据体系。所有存储对象（memory/inbox/runtime/approval/overflow）路径由 BotService 统一切换，框架层无感知。

**Architecture:** 新增 `framework/workspace/` + adapter 拦截 + 业务层 callback。cd/exit 在 adapter 层被拦截，active_checker 确保无 agent 运行时才执行切换。切换时 callback 按顺序更新所有存储对象路径。

## File Structure

| Action | File | Responsibility |
|---|---|---|
| Create | `framework/workspace/models.py` | CdResult, WorkspaceSwitchCallback |
| Create | `framework/workspace/parse.py` | parse_user_path（pathlib + \→/ 归一化） |
| Create | `framework/workspace/context.py` | WorkspaceContext ABC + DefaultWorkspaceContext |
| Create | `framework/workspace/handlers.py` | Cd/Exit/Pwd handlers（NOTICE + English） |
| Create | `framework/workspace/__init__.py` | 模块导出 |
| Modify | `framework/commands/constants.py` | BuiltinCommand + CD/EXIT/PWD |
| Modify | `framework/pipeline/pipeline.py` | has_active_sessions() |
| Modify | `framework/pipeline/adapters.py` | _try_intercept_control 拦截 cd/exit |
| Modify | `examples/bot_project/bot/service/core.py` | WorkspaceContext + callbacks + _ws_* helpers + _init_long_term_defaults fix + _update_pruned_manager |
| Modify | `examples/bot_project/bot/service/pool_builder.py` | data_dir 必填 |
| Modify | `examples/bot_project/bot/service/builders.py` | subagent 路径更新 |
| Create | `tests/unit/workspace/test_models.py` | 4 tests |
| Create | `tests/unit/workspace/test_parse.py` | 11 tests |
| Create | `tests/unit/workspace/test_context.py` | 25 tests |
| Create | `tests/unit/workspace/test_handlers.py` | 12 tests |
| Create | `tests/unit/workspace/test_has_active_sessions.py` | 5 tests |
| Create | `tests/unit/workspace/test_context_manager_switch.py` | 2 tests |
| Create | `tests/unit/workspace/test_cd_directory_structure.py` | 1 test |

## Task 1: Models — CdResult + WorkspaceSwitchCallback
## Task 2: Path Parse — parse_user_path
## Task 3: Core — DefaultWorkspaceContext
## Task 4: Pipeline.has_active_sessions()
## Task 5: Handlers — Cd/Exit/Pwd
## Task 6: Module Exports
## Task 7: Adapter Layer Interception
## Task 8: Business Layer Integration
- WorkspaceContext 注入 + active_checker
- _ws_memory/_ws_runtime/_ws_approval/_ws_inbox 路径入口
- 3 回调：BackgroundStop → MemoryRebuild → TerminalReset
- pool_builder data_dir 必填

## 安全验证

**active_checker 两层保护：**
1. Pipeline: `has_active_sessions()` — `_session_tasks` task done 检查
2. Pool: `_active_session_counts[agent] > 0` — dispatch 计数

**Adapter 拦截**: cd/exit 在 `_try_intercept_control` 处理，不进 pool dispatch，不自增计数。

**测试**: 144 passed ✅

## 知识初始化修复 (2026-06-05 后续)

**Bug 1**: `_init_long_term_defaults` 只检查 `long_term`（旧配置），YAML 使用 `knowledge`（新配置），
导致硬编码默认值从未被调用，knowledge 初始化依赖 `get_all()` → `ensure_defaults()` 的副作用。

**Bug 2**: `default_templates_dir: "templates/knowledge"` 相对 CWD 解析。cd 后 `os.chdir()`
改变 CWD，模板找不到，`ensure_defaults` 写入空文件。

**Bug 3**: `FullInjectionPolicy._pruned_manager` 缓存引用过期。cd 后 MemorySystem 被替换，
injection policy 仍指向旧 pruned_manager → pruned 数据写到旧路径。

**修复：**
1. `_init_long_term_defaults` 同时检查 `knowledge` 和 `long_term` 两种配置格式
2. 模板目录相对于 `_project_dir` 解析为绝对路径，更新 knowledge manager config
3. 新增 `_update_pruned_manager()` 辅助函数，在 pipeline 和 pool 回调中同步 injection policy

**已验证**: Archive / User retention buffer / Inbox / Runtime stores / Approval / Overflow store
均随 `create_memory()` 自动切换或通过 callback 显式更新，无需额外修复。
