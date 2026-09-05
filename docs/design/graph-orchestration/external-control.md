# 图外部控制与恢复设计（External Control & Recovery）

Status: **current**（按当前实现更新，2026-09-05）
Date: 2026-09-05

本文档记录 `modex_graph` / `modex_agent` 图编排当前实现的**外部控制面**（暂停 / 停止 / 恢复 / 外部投递）与**崩溃恢复**契约。与 `distributed-persistence.md`（持久化层文档）互补；历史实施 ticket 见 §13，验证范围见 §14。

## 1. 背景与目标

`GraphOrchestrator` 是实例执行的唯一 owner：同步准入、任务保留、控制信号、最终状态和资源释放由同一个生命周期负责。REST 请求、控制命令和恢复扫描不维护第二份执行注册表，也不预写状态来冒充执行。

暂停是**在同一 graph instance 上立即发起协作式取消并等待实际排空**，不是仅写 PAUSED，也不是等待节点正常跑完。resume 保留实例与逻辑 run 身份，创建新的执行 attempt；恢复与正常执行复用组装和调度路径，差异由显式 `BootstrapMode` 决定。

## 2. 图实例状态机语义

`GraphInstanceStatus` 包含待执行、运行、过渡和已结束状态：

| 状态 | 含义 | 进入路径 | 恢复行为 |
|------|------|---------|---------|
| PENDING | 已创建，尚未准入执行 | `create_instance` | `start_run` 使用 FRESH |
| RUNNING | 已准入，owner task 已保留；可能尚未进入节点 | 新执行 / 恢复准入 | 不自动恢复；本地无 owner 不能证明原进程已死 |
| PAUSING | 暂停请求已接受，取消和清理尚在进行 | owner 写入后请求 pause | 不可 resume / 重入 / eviction |
| STOPPING | 停止请求已接受，尚在排空 | stop 或 pause 升级为 stop | 不可 resume / 重入 / eviction |
| PAUSED | 节点排空完成，或 HITL `GraphInterrupt` 退出；最终输出仍可能在排空 | owner finalization | owner 退出后才可显式 resume，不自动恢复 |
| STOPPED | 人为终止，终态 | owner 排空完成 | 不可 resume / re-invoke，不被扫描 |
| CRASHED | 异常退出或已确认的进程死亡 | owner 异常 / 外部进程分类器 | 自动恢复候选；也可显式 FRESH re-invoke |
| COMPLETED | 正常到达 END | owner finalization | 可显式 FRESH re-invoke，不自动恢复 |
| FAILED | 调度结束但未到达 END，或业务层判定失败 | owner dead-end / 业务层策略 | 可显式 FRESH re-invoke，不自动恢复 |

图级与节点级状态分开：取消整个执行 task 会使图为 CRASHED，但收到 `CancelledError` 并执行清理的节点为 CANCELED。真正进程终止未运行清理的节点可能遗留 RUNNING，恢复时处理孤儿记录。框架不维护重试预算；业务层自行决定何时重试或放弃。

## 3. GraphRunControl：run 级控制面

`GraphRunControl`（`modex_graph`，不依赖 `modex_agent`）是 `ctx.control`，**per-run 一个实例**；owner 在准入时创建，因此节点尚未开始也能收到控制请求。pause/stop 标志不可撤销，resume 创建新 handle。

三个部件：

1. **单向标志**：`request_pause(reason)` / `request_stop(reason)`；只读属性 `pause_requested` / `stop_requested` / `drain_reason`。
2. **唤醒**：pause/stop 和 `notify_deliver(target)` 都 set 唤醒事件。`wait_for_tasks()` 同时等待节点任务与控制/投递活动；已完成任务中的异常先于控制 drain 信号传播。
3. **排空**：`check()` 命中标志抛 `GraphDrained(reason)`；scheduler 的 `finally` 调 `cancel_and_drain()`，只取消尚未取消的任务并 await 所有任务的异步清理。`wait_for_settlement()` 是 scheduler drain 和 owner finalization 共用的等待边界：重复取消等待方不会再次取消清理，取消延迟到清理结束后传播，清理故障优先于取消或暂停信号。

`GraphDrained(GraphBubbleUp)` 表达预期控制退出，不是故障。owner 此时仍保留执行，直到 finalization 完成才释放；GraphDrained 映射为 PAUSED，有 stop 请求时升级为 STOPPED。

`RESUME_GRAPH` 委托 `GraphOrchestrator.resume()` 重新准入，不撤销旧 control 上的标志。

## 4. 调度器安全点与暂停语义

两种 scheduler 都将节点执行放在自己持有的 task 中，通过 `wait_for_tasks()` 在节点阻塞于 await 时响应控制。主循环和进入节点前的检查阻止暂停后启动 READY 节点；退出时通过 `cancel_and_drain()` 收敛取消与等待。

1. 请求时停止新增节点执行，并取消在途节点，而非等业务工作正常完成。
2. 等待节点的 `finally`、异步资源清理和其下层 owner 的 drain。
3. scheduler 退出后，orchestrator 排空 coordinator 的节点事件和已排队状态事件，再落 PAUSED / STOPPED、finalize 图 invocation、等待最终状态输出。
4. owner task 退出才释放准入占位。`pause()` / `stop()` shield 等待此 task；HTTP 等待者取消不会再次取消节点清理，也不会释放 owner。

**节点状态**：`Node.run()` 捕获 `GraphBubbleUp`（含 HITL）或 `asyncio.CancelledError`，调用 `cancel_invocation` 记录 CANCELED；其他 Exception 记录 CRASHED；`finally: finalize_invocation` 仅为孤儿 RUNNING 提供 CRASHED 兜底。取消不是节点故障。

**边界**：这是 asyncio 协作取消，不会抢占同步阻塞代码，也不会回滚已经发生的外部副作用（工具写入、网络请求或 provider 已接受的工作）。清理未完成时 pause/stop 不承诺已完成，也没有调用方超时来伪装 drain。Bot agent 节点的 session-tree 清理由 `BotAgentNode` / `SessionTreeManager` 负责，图层等待节点退出，不另建 session 完成追踪器。

## 5. 执行所有权与控制适配

- **同步准入**：`start_run` / `start_invoke` / `start_resume` 验证状态、`begin_invocation` 并保留 execution/control 后，eager 启动 owner task，使其先进入 `try/finally` 再暴露给调用方。正常准入在首次挂起前持久化逻辑 run 身份、I/O 记录并写 RUNNING；引擎启动前等待 RUNNING 输出。组装失败或调用方立即取消仍由该 owner finalization，首次调度前也不能重复启动。`run_instance` 等待同一准入路径。
- **共享组装**：`_execute` 编译 spec、还原 node_id、复用已保留 coordinator 或从 store 重建、构造 context，并按显式 mode 执行。`get_graph_context(gid)` 在 finalization 结束前可取活动 context。
- **唯一写入者**：`GraphControlService` 只把生命周期命令委托给 orchestrator；没有 `_engines` 注册表或独立状态写入。导出的 Live/Recording controller 只是信号/记录适配器，不是 orchestrator owner。
- **状态事件**：owner 持久化并串行发送 `GraphOutputKind.STATUS_CHANGED`，携带 typed `status`。RUNNING / PAUSING / STOPPING / PAUSED / STOPPED 走此路径；正常完成、失败、异常仍有各自的 terminal output。节点事件先排空，最终暂停状态后发。
- **竞态**：重复 pause/stop 等待已有 owner；已 PAUSED 的 pause、已 STOPPED 的 stop 幂等。stop 可升级正在排空的 pause，甚至在 PAUSED 输出等待中升级。已确定的 COMPLETED / FAILED / CRASHED / STOPPED 结果不会被迟到的 pause 改写。
- **资源**：PAUSED 保留 coordinator 供进程内恢复。`cleanup()` 先 `pause_all_active()` 等待执行，再释放空闲 coordinator；`unregister_instance()` 拒绝移除运行或排空中的 owner。其他已结束执行释放 coordinator。
- **外部投递**：`deliver_to_node` 验证实例为 PENDING / RUNNING / PAUSED，经 coordinator `route_deliver(stage=False)` 写入目标 store 后唤醒 control。PAUSING / STOPPING 拒绝投递。只有 metadata RUNNING、没有本地 owner 的实例不能据此获得 pause/stop 权限。

Linear 是单指针内部流，不支持运行中外部 deliver 的多源准入；外部投递调度用于 Parallel 图。跨进程控制通道不在当前实现内。

## 6. 级联崩溃契约

**node 崩溃 = graph 级联崩溃，除非 node 自己 catch。** Parallel 任一实例异常会取消并排空其他在途 task，然后向 owner 传播异常，图标为 CRASHED；Linear 同样传播节点异常。

- 故障节点为 CRASHED，被协作取消的 sibling 为 CANCELED，恢复时两者都可重派。
- `GraphDrained` 是控制面信号，由 scheduler 在安全点主动抛出，**不属于级联路径**。
- 节点内部 catch 并消化的异常框架不可见——错误处理是节点业务职责，与幂等职责同源。

## 7. 恢复入口集推导（两 scheduler 共享）

两 scheduler 共用 `bootstrap(ctx, graph, mode=...)`：FRESH 直接返回 entry，不扫描历史。RECOVERY 使用显式逻辑 run 归属，不从 Snowflake 大小、时间戳或 START/END 主键推断执行先后。

**逻辑 run 与 attempt**：每次图执行 attempt 都有新的 `GraphMetadata.version` / `GraphIORecord.version`。FRESH 将该图 version 写入 `GraphMetadata.attrs[GRAPH_RUN_VERSION_KEY]`（键值为 `graph_run_version`）；RECOVERY 保留这个初始值，不换成恢复 attempt 的 version。`Node.run()` 将 `ctx.graph_run_version` 传给 `begin_invocation`，保存在 `InvocationContext` / `NodeInvocationRecord.graph_run_version`；I/O 同样保存 `GraphIORecord.graph_run_version`。它与节点自身不断递增的 `version` 是不同维度。

**精确相等**：这些 membership 字段为 `int | None`。缺失 attr 和旧的未分组记录使用 None；None 只与 None 相等，不是通配符。SQLite 的 `node_states` / `graph_io_records` 保存 nullable INTEGER，旧表补列后旧行保留 NULL；InMemory 保留模型字段。store 仍按 invocation version 取 latest，再由恢复代码检查 membership，而非按主键范围查找或回填一个猜测的 run。

1. `ctx.graph_run_version` 非 None 时，先检查 entry（START）的 latest invocation。缺失或 membership 不相等就置 `reached_end=False` 并返回 entry，尚不做历史 promotion 或 deliver 扫描。旧 run 即使全部完成，也不能证明当前 FRESH run 已经开始。
2. 每节点取 `load_latest`，仅保留 `record.graph_run_version == ctx.graph_run_version` 的记录。对其中 COMPLETED 节点补做 STAGED 输出和 CONSUMED_PENDING 输入 promotion，覆盖完成与 promotion 之间的崩溃窗口。
3. 匹配记录中的 CRASHED / 孤儿 RUNNING / CANCELED 作为重执行候选；COMPLETED 本身不是重执行理由。CANCELED 即使没有输入也可重入；有输入时重消费 CONSUMED_PENDING。
4. PENDING deliver 的目标也纳入候选，包括从未执行的节点，或收到新输入的已完成节点。deliver 消费/扫描契约不变；membership 检查不是新增的 deliver run 过滤器。入口按从 entry 的 BFS 排序。
5. 无候选且有匹配 run 的 invocation 历史才返回空集；无候选且无匹配历史（包括 Null stores）返回 entry。这不是“任意历史都已完成就永不再执行”。Linear 取最早入口后按正常路由执行；Parallel 保留重执行入口的输入 ID，避免 store scan 为同一输入重复调度，投递入口复用正常 trigger 准入。

**END 与 I/O**：恢复入口包含 END，或无候选且匹配 run 的 END 已完成时，bootstrap 保留 `reached_end`。owner 只接受 membership 相等的 latest I/O；恢复 attempt 的占位记录携带同一 run 的输入与已有输出，避免连续恢复在引擎开始前中断时丢失结果。匹配的 completed END 未变化且已有同 run I/O 时沿用其输出，不为重建结果重放 END；本次 END 新完成但 pause/cancel 抢先被调度器观察到时，也保存该输出。FRESH re-invoke 不继承上一 run 的 I/O；只有 PENDING 实例的首次 FRESH 可沿用 `create_instance` 提供的输入。

明确**不做**的推断：

- **不做来源过滤**：不因"deliver 来自非 COMPLETED 的源 invocation"丢弃它。源节点重跑后是否再次 deliver 由源节点自己的幂等实现决定（见 §8），框架丢弃可能丢掉目标唯一的触发源。
- **不做入度推断**：不以"上游全部完成"推断目标该新增一次调用。上游可能因条件分支有意不投递（D10 silent skip）。新投递触发以 PENDING deliver 为凭证；未完成 invocation 的重执行由版本链独立决定，不能用缺少输入排除 CANCELED 等候选。

**时序不变量**：节点 `execute` 内 deliver 先持久化为 STAGED；`complete_invocation` 后才 promote 输出为 PENDING、dispatch 唤醒目标、promote 消费的输入。因此完成记录不保证 promotion 已结束，RECOVERY 需补做；未完成源的输出不直接对目标可消费，重跑后可能产生重复内容。

调度队列不持久化；恢复依据 invocation + deliver 事实，不依赖 `resume_target` 或持久化业务 state。`ctx.state` 由调用方初始化，不恢复执行栈或中间 snapshot。

## 8. at-least-once 契约与节点幂等

**框架契约**：持久化恢复路径的投递与未完成节点工作是 at-least-once，包括暂停后恢复的 agent 工作。重新调用节点可能重放 LLM/provider 请求或工具副作用；保留同一 graph/run 身份不等于恢复执行栈，也不提供 exactly-once 或外部副作用回滚。

源节点 A 写入 STAGED 后失败，重跑又写入相同内容时，完成后的 promotion 可能让 B 消费两份。**框架不按内容去重**；deliver 消费状态只防止已确认消费的记录重复消费，不保证业务副作用 exactly-once。

**幂等是节点业务侧职责**（`issues/history/10-graph-lifecycle-management.md` 既有决议）：框架不传递"调用原因"信号（正常新一轮 / 崩溃重跑 / 重投不区分），不提供 `is_retry` / `idempotency_key` 机制。框架提供的幂等原语（已存在，节点可用）：

- `ctx.node_state_store.load_latest(self.node_id)` / `query_versions(...)`：查自己的 invocation 状态与版本链，不含业务 snapshot；
- `ctx.current_invocation.version` / `parent_version`——"这是我第几次被唤醒"；
- 节点自己的持久化业务记录：保护跨重执行副作用；`ctx.state` 仅是当前运行工作区；
- deliver 四态消费状态机（STAGED / PENDING / CONSUMED_PENDING / CONSUMED_COMPLETED）。

**HITL 也走重新调用**：`GraphInterrupt` 使节点 CANCELED、图 PAUSED，并向调用方传播。resume 建立新 invocation 并重消费输入；节点应根据已持久化输入或自己的业务记录决定继续或再次 interrupt，不承诺 suspend-without-re-execution。

## 9. 持久化档位降级矩阵

恢复能力由实例 metadata 与节点/deliver stores 共同决定，混用档位不能仅凭图状态推断恢复能力：

| 能力 | SQLite | InMemory | Null |
|------|--------|----------|------|
| 进程内异常/暂停恢复 | invocation + 四态 deliver 推导 | stores 仍存活时可重消费 CONSUMED_PENDING | 无节点历史，bootstrap 回到 entry |
| 进程重启恢复 | 同一 DB 重建 stores | 数据不跨进程保留 | 不支持 |
| `recover_crashed()` | 扫显式 CRASHED | 扫仍存活实例 store 的 CRASHED | Null instance store 返回 `[]` |
| `resume(gid)` | idle PAUSED 可恢复 | metadata 与 stores 仍在时可恢复 | 保留的 PAUSED runtime 可准入，但不具有节点历史恢复保证 |

`_metadata` 可从活动 runtime 读取 Null instance store 没有保存的状态；runtime 释放后找不到实例会抛 `ValueError`，不是 `InstanceNotFoundError`。InMemory metadata + Null coordinator 也可重建并调用 resume，但节点会从无历史入口重跑；这不是持久化恢复验证。

graph 持久化管调度连续性，node 自己的持久化管业务连续性；两者正交。业务层需要跨重启恢复时应装配 SQLite 实例、节点、deliver stores，需保存/复用输入输出时还应配置 I/O store。

## 10. 恢复扫描与重试预算：归业务层

框架只提供调度接口，不含任何定时器与计数器：

- `GraphOrchestrator.recover_crashed()`：只扫显式 CRASHED，逐一交给正常准入路径。自动恢复记录执行失败并继续其他候选；`GraphInterrupt` 仍传播。手动 resume 的验证/执行错误直接传播。
- `GraphOrchestrator.resume(gid)`：手动恢复，只接受 PAUSED（见 §2）。
- 业务层先以 executor/process liveness 分类遗留 RUNNING / PAUSING / STOPPING，再标 CRASHED；本地没有 `_Execution` 不是进程死亡证据。扫描时机、重试预算和 PAUSED 的恢复策略均由业务层决定。

手动 resume 只接受 idle PAUSED，保留 graph_instance_id、node_id、初始 `graph_run_version` 和 store 事实；同进程复用 coordinator，重建 owner 从 store 组装。`_run_existing_instance` 只是 `run_instance(mode=RECOVERY)` 委托，不先 eviction、不预写 RUNNING。新的图 invocation 在同步准入时创建，旧 invocation 历史保留；attempt version 的递增不改变逻辑 run 归属（§7）。

最小 SQLite 装配（`RecoveryScanner.run()` 启动时立即扫描，之后按间隔扫描；连接由业务层最终关闭）：

```python
import sqlite3

from graph_patterns.recovery_scanner import RecoveryScanner
from modex_agent.orchestration import GraphOrchestrator, SqliteCoordinatorFactory
from modex_graph import SqliteGraphInstanceStore, SqliteGraphSpecStore

db_path = workspace_root / ".modex" / "state.db"
db_path.parent.mkdir(parents=True, exist_ok=True)
connection = sqlite3.connect(db_path)
spec_store = SqliteGraphSpecStore(connection)
instance_store = SqliteGraphInstanceStore(connection)
orchestrator = GraphOrchestrator(
    node_registry=node_registry,
    state_classes=state_classes,
    spec_store=spec_store,
    instance_store=instance_store,
    coordinator_factory=SqliteCoordinatorFactory(connection),
)
scanner = RecoveryScanner(
    orchestrator,
    instance_store,
    interval_seconds=30,
    max_recovery_attempts=3,
)
try:
    await scanner.run()
finally:
    await orchestrator.cleanup()
    connection.close()
```

## 11. 不做的事

- **不做"节点执行到一半"的恢复**：可恢复性锚定在节点边界，不捕获 in-flight 中间状态，不因此强求所有节点幂等。
- **不加 `recovery_attempts` 等框架级计数/预算**（归业务层，§2/§10）。
- **不加 DRAINED 状态**：用 PAUSING / STOPPING 表示未完成的排空，用 PAUSED / STOPPED 表示完成的控制结果。
- **不做 deliver 来源过滤、不做入度推断**（§7）。
- **不给节点传"调用原因"信号、不提供 `is_retry`/`idempotency_key`**（§8，既有决议延续）。
- **不改 `Scheduler` ABC**：`check()` 调用是 scheduler 内部实现细节，不是 ABC 契约。
- **不清理 `ctx.fork()` 等死 API**：另立 ticket 跟踪，不混入本期。
- **不做跨进程控制通道**（Redis 等）：当前控制只面向本地 execution owner。

## 12. 设计取舍说明

关键取舍是**立即发起取消，真实等待清理**：暂停可打断节点 await，但不能抢占同步代码；PAUSING / STOPPING 明确区分请求已接受和 drain 已完成。请求等待者不是执行 owner，取消 HTTP 等待不会替代生命周期 finalization。

恢复复用 invocation/deliver 路径，CANCELED、CRASHED 和孤儿 RUNNING 都会建立新 invocation；HITL 也不恢复执行栈。代价是节点必须用输入或自己的持久化状态保护业务副作用，框架不提供中途 snapshot 恢复。

未执行任务的持久化策略是**不落盘调度队列、恢复时从版本链 + PENDING deliver 重新派生**。入口推导后的 trigger 准入仍由正常 scheduler 执行，不维护另一份恢复调度队列。

## 13. 实施 ticket 索引

| Ticket | 内容 | 依赖 |
|--------|------|------|
| `issues/34-graph-run-control.md` | `GraphRunControl` + `GraphDrained` 激活 + 两 scheduler 安全点 + 唤醒戳 | — |
| `issues/35-live-engine-controller.md` | `LiveGraphEngineController` + orchestrator `GraphDrained` 映射 + `deliver_to_node` 唤醒 | 34 |
| `issues/36-recovery-entry-derivation.md` | 共享恢复入口集推导 + LinearScheduler 恢复统一 + 时序不变量测试 | — |
| `issues/37-instance-status-semantics.md` | STOPPED 终态化 + `resume` 只认 PAUSED + 状态语义测试 | — |
| `issues/38-sqlite-coordinator-wiring.md` | SQLite `CoordinatorFactory` 装配 + orchestrator 生产接线 + 业务层扫描器示例 | 37 |
| `issues/39-recovery-control-e2e.md` | 暂停-取消-恢复 / 崩溃重叠 deliver / Linear 跳过已完成 / Null 档 fail-safe 的 E2E 测试 | 34, 35, 36, 37 |

## 14. 本次验证范围（2026-09-05）

以下为本会话较早的测试对齐阶段实测结果，不是最终 `graph_run_version` 实现的全量验收。本次最终同步只核对源码与文档，未运行或代报父任务正在执行的框架测试。

- `rtk pytest tests/integration/graph_orchestration/ -m integration -q`：27 passed。修改前 5 个失败均为协作取消后节点 CANCELED 与旧 CRASHED 断言不符；保留图级 CRASHED、恢复后的执行次数、输入、版本链和完成状态断言。
- `rtk pytest examples/bot_project/tests/webui/test_graph_routes.py -q`：54 passed。重建 owner 的测试通过真实 `cleanup()` 释放旧资源，重建 orchestrator 后 await `start_resume()`，不清私有运行集合、不用 sleep 推断完成。
- `test_pause_instance` / `test_stop_instance` 已改为真实 owner 运行阻塞节点，HTTP 请求在节点异步清理期间保持等待，分别观测 PAUSING / STOPPING；释放清理后才返回 PAUSED / STOPPED，并确认活动 context 已释放。无手写 RUNNING 状态或生产控制行为修改。
- `rtk pytest examples/bot_project/tests/webui/test_ws_graph_subscription.py -q`：8 passed，验证 graph WebSocket 订阅、投递与隔离。
- 重建测试使用 InMemory metadata + Null coordinator，仅验证 owner 重建和异常传播；SQLite 集成恢复使用 task cancellation 后关闭/重开连接，不是真实进程 kill。此次未执行 live bot、真实 provider、浏览器或强杀进程验证；以上实现说明不等于这些路径均已端到端验证。
