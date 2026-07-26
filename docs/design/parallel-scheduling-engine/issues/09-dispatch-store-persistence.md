# 09 — 持久化:DispatchStore ABC + SQLite 实现

**What to build:**

引入 `DispatchStore` ABC 作为 dispatch 事件的持久化抽象层,提供 dispatch 事件的存储和查询接口。实现 SQLite 适配器(建表语句、索引设计由实现决定,需考虑按 target 查询、按 source_instance 查询、按图运行 ID 查询)。`ParallelScheduler` 默认使用内存实现,可选切换到 SQLite 实现。设计需同时考虑文件后端的未来扩展(本次只实现 ABC + 内存默认 + SQLite 适配器)。所有字符串常量枚举化,表名/列名通过常量或枚举管理,避免硬编码。

**Blocked by:** 03

**Status:** completed

- [x] `DispatchStore` ABC 定义(放在 `modex_graph` 包内,保持框架无关):
  - `record(event: DispatchEvent, run_id: str) -> None` — 记录一条 dispatch 事件
  - `query_by_target(target: str, run_id: str) -> list[DispatchEvent]` — 查询某次图运行中投递给某 target 的所有 dispatch
  - `query_by_source(source_instance: str, run_id: str) -> list[DispatchEvent]` — 查询某 source 实例发出的所有 dispatch
  - `query_all(run_id: str) -> list[DispatchEvent]` — 查询某次图运行的所有 dispatch
  - `clear(run_id: str) -> None` — 清除某次图运行的 dispatch 记录
- [x] `InMemoryDispatchStore` 默认实现:使用 `dict[run_id, list[DispatchEvent]]` 内存存储。`ParallelScheduler` 默认使用此实现
- [x] SQLite 适配器实现(放在 `modex_graph` 包内,依赖 `aiosqlite` 或 `sqlite3`——注意 `modex_graph` 当前只依赖 Pydantic + stdlib,SQLite 适配器可能需要作为可选依赖或放在独立模块):
  - 建表语句设计(由实现决定,但需考虑):`dispatch_events` 表,列包含 `run_id`(TEXT)、`source_instance`(TEXT)、`target`(TEXT)、`payload`(TEXT/JSON)、`seq`(INTEGER,全局序号)、`created_at`(INTEGER ms,per ADR-0029)
  - 索引设计(由实现决定,但需考虑):`idx_dispatch_run_target(run_id, target)`、`idx_dispatch_run_source(run_id, source_instance)`、`idx_dispatch_run(run_id)`
  - `run_id` 作为图运行的唯一标识(一次 `GraphEngine.run_async` 调用对应一个 run_id)
  - payload 的 JSON 序列化/反序列化(payload 是 `dict[str, Any] | None`)
- [x] `ParallelScheduler` 构造时接收 `dispatch_store: DispatchStore | None = None`;`None` 时使用 `InMemoryDispatchStore`。`GraphEngine` 或 `GraphContext` 携带 `run_id` 供 store 使用
- [x] `GraphContext.dispatch` 调用 `dispatch_store.record(event, run_id)` 记录 dispatch 事件(在内存模式下也是记录,只是存在内存 dict 里)
- [x] dispatch 事件的 `DispatchEvent` 模型(在 03 中定义)确保可序列化:所有字段是基本类型(str, dict, None),可通过 JSON 序列化
- [x] 测试:`InMemoryDispatchStore` 的 record / query_by_target / query_by_source / query_all / clear 行为正确
- [x] 测试:SQLite 适配器的 record / query_by_target / query_by_source / query_all / clear 行为正确(使用临时数据库文件)
- [x] 测试:`ParallelScheduler` 使用 `InMemoryDispatchStore` 时 dispatch 事件被正确记录
- [x] 测试:`ParallelScheduler` 使用 SQLite 适配器时 dispatch 事件被持久化,图运行结束后可查询
- [x] 测试:payload 为 None 的 dispatch 事件正确存储和读取
- [x] 测试:多次图运行(run_id 不同)的 dispatch 事件互不干扰
- [x] 表名、列名通过模块级常量或枚举管理,不硬编码 SQL 字符串拼接;使用参数化查询防注入
- [x] `DispatchStore` ABC 遵循项目 rule 7(ABC before implementations, zero Protocols);`DispatchEvent` 遵循 rule 10-16(Pydantic BaseModel, frozen, extra="forbid")
- [x] SQLite 适配器的迁移脚本(建表)遵循 ADR-0023 的 `MigrationRunner` 模式:有序 SQL 文件,`schema_migrations` 表追踪,每迁移一个显式事务。如果 `modex_graph` 不依赖 `modex_agent` 的 persistence 层,则在 `modex_graph` 内提供独立的轻量迁移机制(或直接在适配器 `__init__` 中执行 `CREATE TABLE IF NOT EXISTS`)
- [x] 时间戳遵循 ADR-0029(epoch millisecond, `now_ms()`)。如果 `modex_graph` 不能依赖 `modex_agent.utils.time`,则在 `modex_graph` 内定义等效的 `now_ms()` 工具函数
- [x] 文件后端的未来扩展预留:`DispatchStore` ABC 的接口设计不依赖 SQLite 特性(如 `rowid`),确保未来 `FileDispatchStore` 实现可行
