# 图外部控制与恢复设计（External Control & Recovery）

Status: **current**（设计权威，2026-08-05 设计讨论定稿）
Date: 2026-08-05

本文档定义 `modex_graph` / `modex_agent` 图编排的**外部控制面**（暂停 / 停止 / 恢复 / 外部投递）与**崩溃恢复**的完整设计。与 `distributed-persistence.md`（持久化层权威）互补：本文档管控制面与恢复语义，持久化文档管 store 与生命周期。实现 ticket 见 `issues/34`~`39`。

## 1. 背景与目标

图引擎的持久化与恢复机制（三表 store、版本链、deliver 消费状态机、`load_for_recovery`）已建成并有测试覆盖，但外部控制面存在三个缺口：

1. **运行中的图收不到暂停/停止信号**：`GraphControlService` 已收敛 `PAUSE_GRAPH / STOP_GRAPH / RESUME_GRAPH / DELIVER_TO_NODE` 命令并持久化状态，但 `InMemoryGraphEngineController` 是纯记录 stub（`graph_control.py` 注释：*"A `LiveGraphEngineController` that wires pause/stop/resume into the scheduler loop is deferred"*），调度循环无感知。
2. **恢复能力未接线**：`GraphOrchestrator` 无生产实例化点，默认 `NullCoordinatorFactory`（无持久化），`recover_crashed()` 无生产调用方。
3. **LinearScheduler 崩溃恢复语义弱**：恢复时从 entry 重跑，不跳过 COMPLETED 节点（HITL 的 `resume_target` 路由不是崩溃恢复机制）。

设计目标：把 ReAct 已验证的"channel + 安全点 drain"协作式中断范式（`drain_control_channel` + `ControlDrainInterceptor`）下沉到图调度层，用最小新增组件补齐外部控制；恢复与正常执行**复用同一条路径**，差异只在入口集推导。

## 2. 图实例状态机语义

`GraphInstanceStatus` 六态不变，语义精化如下：

| 状态 | 含义 | 进入路径 | 恢复行为 |
|------|------|---------|---------|
| RUNNING | 调度中 | `create_and_run` / 恢复派发 | recover 扫描会捡（进程死亡留下的孤儿 RUNNING） |
| PAUSED | **人为暂停**，可手动恢复 | pause 命令（`GraphDrained` 退出） | 不被自动恢复；只接受 `resume(gid)`（业务层决定时机） |
| STOPPED | **人为终止，终态** | stop 命令 | 不可 resume，不被扫描 |
| CRASHED | 异常退出（含未捕获异常传播、进程死亡），**可重试** | orchestrator `except Exception` / 节点级联崩溃 | recover 扫描可捡；重试预算与放弃决策归业务层 |
| COMPLETED | 正常完成，终态 | scheduler 正常返回 | 终态 |
| FAILED | 业务判定的永久失败，终态 | **业务层**在重试预算耗尽后经 `update_status` 写入 | 终态 |

要点（2026-08-05 决议）：

- **未捕获异常退出 → CRASHED 而非 FAILED**（现状已如此，`graph_orchestrator.py` `except Exception → CRASHED`）。CRASHED 语义 = "仍有机会重试"。
- **FAILED 不由框架写入**。框架不提供重试计数与预算（`GraphMetadata` 保持 5 字段，不加 `recovery_attempts`）。业务层在扫描时自行计数、自行决定放弃并标 FAILED（经 `GraphInstanceStore.update_status` 这条既有单一写入路径）。业务层甚至可以不区分 crashed/failed/completed——框架状态机不强加业务语义。
- **STOPPED 终态化**：`GraphRecoveryService.resume()` 只接受 PAUSED（改掉现行"PAUSED 或 STOPPED"）。用户停 ≠ 系统失败，两终态分开。
- 人为暂停不被自动恢复（`recover_crashed` 只查 CRASHED + 孤儿 RUNNING，现状已如此）——"暂停 ≈ 人为 crash"仅指节点级状态落法相同，图级恢复触发权始终在业务层。

## 3. GraphRunControl：run 级控制面

新增 `GraphRunControl`（`modex_graph`，框架无关，不依赖 `modex_agent`），作为 `GraphContext` 的字段（`ctx.control`），**per-run 一个实例**。设计要点：单向、无锁（单属性写入即原子）、不可撤销。

三个部件：

1. **单向标志**：`request_pause(reason)` / `request_stop(reason)`；只读属性 `pause_requested` / `stop_requested` / `drain_reason`。
2. **唤醒戳**：`notify_deliver(target)`——外部 `deliver_to_node` 命中**运行中**的图时，deliver 走 `route_deliver` 落 DeliverStore（现有路径不变），再戳 control 唤醒调度循环按目标节点的 trigger 模式消化。这补上现有缺口：`GraphControlService.deliver_to_node` 目前对运行中的图写了 store 但调度器永远不知道。
3. **单一 drain 点**：`check()`——scheduler 只在安全点调用；命中标志抛 `GraphDrained(reason)`。未来需要带参数的命令（如向 agent 节点注入 steer 消息）时在 control 内加命令 deque、`check()` 里 drain，**scheduler 调用点不变**（ReAct `drain_control_channel` 模式下沉）。

`GraphDrained(GraphBubbleUp)` 已存在（`exceptions.py`），D7 保证 scheduler/engine 永不吞没。一种异常覆盖 pause 与 stop：目标状态由 control service 在调 engine **之前**已写入（`_pause`→PAUSED、`_stop`→STOPPED），`GraphDrained` 上抛只表达"预期内退出"，`reason` 仅供观测。

`RESUME_GRAPH` 不走 control——它针对非运行实例，走 `GraphRecoveryService` 路径（现有）。

## 4. 调度器安全点与暂停语义

**LinearScheduler**：`while` 循环顶部（执行下一节点前）调 `ctx.control.check()`。命中即抛 `GraphDrained`，当前已完成的节点已正常 `complete_invocation` + `promote_delivers`，现场干净。

**ParallelScheduler**：主循环 launch 新 READY 实例前调 `check()`；命中后：

1. 停止 launch 新实例；
2. `cancel()` 全部在途 task（**立即取消，不等收尾**——2026-08-05 决议：暂停就不能继续执行）；
3. 抛 `GraphDrained`。

`LiveGraphEngineController.pause()` 置标志的同时 set 调度器的 `_wakeup` 事件，让阻塞在 `asyncio.wait` 的主循环立刻醒来看到标志。

**在途节点的状态落法（零新增代码）**：`asyncio.CancelledError` 是 `BaseException`，绕过 `Node.run()` 的所有 `except` 直达 `finally: finalize_invocation`，把孤儿 RUNNING 清成 CRASHED（容忍级 CAS）。SQLite store 调用全是同步的，asyncio 取消只落在 `await` 点（`execute()` 内部），不可能撕裂一次状态写入。于是"暂停取消在途节点"在节点级自动等价于 crash——恢复时按入口集规则正常重派。**不为暂停发明新的节点状态。**

## 5. LiveGraphEngineController 与 orchestrator 映射

`modex_agent/control/graph_control.py`：

- 新增 `LiveGraphEngineController` 替换 stub：注册时持有运行实例的 `ctx.control` 引用；`pause()` = `control.request_pause()` + 唤醒戳；`stop()` = `control.request_stop()` + 唤醒戳。
- 注册时机：`GraphOrchestrator._execute` 构造 `GraphContext` 后，把 `ctx.control` 注册进 `GraphControlService._engines`；`_execute` 结束（含异常）时注销。
- orchestrator 异常映射加一条：`except GraphDrained` → **预期内退出，状态不覆盖**（PAUSED/STOPPED 已由 control service 写入），不 re-raise 为错误。与 `GraphInterrupt → PAUSED` 分支并列。
- `deliver_to_node`：现有 `route_deliver` 之后，若实例在 `_active_instances`（运行中），调 `control.notify_deliver(target)`。

跨进程控制（如 Redis channel）不在本期范围；`check()` 轮询式接口形状已为未来留好，引擎侧代码届时不变。

## 6. 级联崩溃契约

**node 崩溃 = graph 级联崩溃，除非 node 自己 catch。** 这是 ParallelScheduler 既有行为（D13：任一实例异常 → cancel 所有在途 task → 异常传播 → orchestrator 标 CRASHED），Linear 单节点天然如此。本文档将其立为显式契约，零代码改动。

- 被取消的 sibling 节点经 `finalize_invocation` 落 CRASHED，恢复时正常重派。
- `GraphDrained` 是控制面信号，由 scheduler 在安全点主动抛出，**不属于级联路径**。
- 节点内部 catch 并消化的异常框架不可见——错误处理是节点业务职责，与幂等职责同源。

## 7. 恢复入口集推导（两 scheduler 共享）

恢复 = 正常执行，唯一差异在入口集。入口集推导规则（fresh start 是"入口集为空 → entry_node"的特例）：

1. **主路径——版本链顶端非终态重派**：每节点取 `load_latest`（版本链顶端，环形反复调用场景天然正确）：CRASHED / 孤儿 RUNNING / suspended RUNNING → 重入候选；COMPLETED / CANCELED → 跳过。
2. **稀有路径——deliver 凭证扫描**：扫所有节点 deliver_store 的 PENDING deliver，目标节点纳入候选——**哪怕它从未有任何 invocation**。适用场景："上游全部完成且已投递，当前节点该执行但还没开始图就崩了"。
3. **触发门判定**（Parallel）：候选按 trigger 模式分流——ON_RECEIVE 直接刷 READY；ON_ALL_PREDS 进 `_pending_dispatches`，由 `_recheck_pending` + `_can_reach_active` 判定（无 active 实例能再到达它 → 刷 READY；否则保持 PENDING 等重派的上游）。**复用运行时同一份触发门逻辑，不为恢复写第二套反向遍历。**

明确**不做**的推断：

- **不做来源过滤**：不因"deliver 来自非 COMPLETED 的源 invocation"丢弃它。源节点重跑后是否再次 deliver 由源节点自己的幂等实现决定（见 §8），框架丢弃可能丢掉目标唯一的触发源。
- **不做入度推断**：不以"上游全部完成"推断节点该执行。上游可能因条件分支有意不投递（D10 silent skip），入度无法区分"该执行没执行"与"本就不该执行"。**deliver 记录是唯一凭证**：有凭证 → 该执行；无凭证 → 等或被跳过，两种情形都不误判。

**时序不变量（立为契约）**：`Node.run()` 先 `submit()`（`route_deliver` 落库）后 `complete_invocation()`，因此 **上游 COMPLETED ⟹ 其 deliver 必然已持久化**。这是"deliver 凭证 ⟺ 上游已提交"推断成立的根基，需配防回归测试。mid-submit 被 kill 的窗口（部分目标收到 deliver）由"源节点重派 + at-least-once"兜底：目标可能收到重复 deliver，由目标节点幂等处理。

**LinearScheduler 统一**：崩溃恢复改走同一套入口集推导（取拓扑序最早候选起步，之后走正常 deliver 路由），替换现行"从 entry 重跑"。`resume_target` 机制保留，但回归本职——只管 HITL 挂起恢复（entry node 读它路由），崩溃恢复不再依赖它。`NodeInstanceStatus`（DORMANT/PENDING/READY/RUNNING/COMPLETED）是 Parallel 的运行时实例状态机， READY/PENDING 的区分只存在于调度簿记层，**持久化的 `InvocationStatus` 不加这两个状态**（调度簿记不持久化、恢复时推导的原则不变）。

## 8. at-least-once 契约与节点幂等

**框架契约**：投递 at-least-once；崩溃影响范围内节点执行 at-least-once。

已知重叠场景的行为声明（当前实现即如此，原属测试空白，本设计将其钉为契约）：源节点 A 重跑 + 目标 B 持有 A 旧 invocation 的 PENDING deliver 时，B 可能消费到 A 的两次 deliver（单次执行双份输入，或两次执行各一份，取决于时序）。**框架不去重。**

**幂等是节点业务侧职责**（`issues/history/10-graph-lifecycle-management.md` 既有决议）：框架不传递"调用原因"信号（正常新一轮 / 崩溃重跑 / 重投不区分），不提供 `is_retry` / `idempotency_key` 机制。框架提供的幂等原语（已存在，节点可用）：

- `ctx.node_state_store.load_latest(self.name)` / `query_versions(...)`——查自己版本链顶端的状态与 snapshot；
- `ctx.current_invocation.version` / `parent_version`——"这是我第几次被唤醒"；
- `ctx.state` 进度标记——推荐模式参考 ToolNode 的 `state.phase` 判定（`react/nodes/tool.py`）；
- deliver 三态消费状态机（框架自动防重复消费，不防 execute 副作用重复）。

**suspend 与 crash 的不对称是刻意的**：HITL 挂起恢复不重跑节点体（ADR-0033 D7，suspend-without-re-execution），崩溃重派会重跑。前者回避了幂等要求，后者没有——有副作用的节点（LLM/工具/外部写入）需要幂等实现，纯计算节点不需要。

## 9. 持久化档位降级矩阵

恢复能力随持久化档位降级，**每条路径的失败方式都必须干净、显式**（"正确地无法恢复"）：

| 能力 | SQLite | InMemory | Null |
|------|--------|----------|------|
| 进程内异常崩溃恢复 | ✅ 完整（三态 deliver、版本链） | ✅ 弱化可用（deliver 二态，崩溃窗口内 at-most-once） | ❌ `recover_crashed` 静默返回 `[]` |
| 进程重启恢复 | ✅ | ❌ 注册表同生命周期，重启后图"从未存在" | ❌ 同上 |
| `resume(gid)` | ✅ | 进程内 ✅ | ❌ 显式抛 `InstanceNotFoundError`（现有行为） |
| `load_for_recovery` | 全量推导 | 进程内有效 | 恒回退 fresh start，正常运行不炸 |

两条原则：

1. **恢复路径 fail-safe，永不幻觉恢复**：无持久化数据时，恢复接口要么静默空转（`recover_crashed → []`），要么显式报错（`resume → InstanceNotFoundError`），绝不退化成"从头跑一遍冒充恢复"。
2. **graph 持久化与 node 业务持久化正交**：graph 持久化管调度连续性，node 自己的持久化管业务连续性。ReAct 是活证据——per-turn 图跑在 NullCoordinator 上，turn 级连续性靠 session memory（modex_agent 持久层），图引擎只是单 turn 执行骨架。

边缘情况声明：Null 档下 pause 一个运行中的图，`GraphDrained` 正常退出但 PAUSED 写入是 no-op，之后 `resume` 抛 `InstanceNotFoundError`——**无持久化的暂停 = 永久停止**。业务层要用暂停/恢复，应配 SQLite 档。框架不加防线，仅此声明。

## 10. 恢复扫描与重试预算：归业务层

框架只提供调度接口，不含任何定时器与计数器：

- `GraphOrchestrator.recover_crashed()`：扫 CRASHED + 孤儿 RUNNING，逐一重建恢复（现有）。
- `GraphOrchestrator.resume(gid)`：手动恢复，只接受 PAUSED（见 §2）。
- 业务层职责：启动时 + 定时调 `recover_crashed()`；对 PAUSED 集合做自己的策略筛选后调 `resume()`；自行维护重试计数与预算，放弃时经 `update_status` 标 FAILED。

恢复内部与正常执行零分叉：入口集推导之外全部复用（`load_for_recovery` 无条件调用、fresh start 是空集特例——现有结构已如此，保持）。

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
    connection.close()
```

## 11. 不做的事

- **不做"节点执行到一半"的恢复**：可恢复性锚定在节点边界，不捕获 in-flight 中间状态，不因此强求所有节点幂等。
- **不加 `recovery_attempts` 等框架级计数/预算**（归业务层，§2/§10）。
- **不加 DRAINED 状态**：pause 退出复用 PAUSED，崩溃退出复用 CRASHED/孤儿 RUNNING，六态够用。
- **不做 deliver 来源过滤、不做入度推断**（§7）。
- **不给节点传"调用原因"信号、不提供 `is_retry`/`idempotency_key`**（§8，既有决议延续）。
- **不改 `Scheduler` ABC**：`check()` 调用是 scheduler 内部实现细节，不是 ABC 契约。
- **不清理 `ctx.fork()` 等死 API**：另立 ticket 跟踪，不混入本期。
- **不做跨进程控制通道**（Redis 等）：`check()` 接口形状已预留，单进程控制句柄先行。

## 12. 设计取舍说明

本设计的关键取舍是**协作式安全点排空**（pause/stop 不中断节点执行中的 await，而在节点边界检查控制标志）+ **立即取消在途任务**（区别于"等在途跑完再停"的优雅排空）。前者保证节点状态机一致性（finalize 兜底），后者保证"暂停即停"的响应性（决策 1）。

恢复语义上采用**suspend-without-re-execution**（HITL 挂起不重跑节点体，ADR-0033 D7）+ **崩溃重派 at-least-once**（崩溃影响范围内节点可能重跑，幂等归节点）。这个不对称是刻意的：HITL 是可预期的挂起点（节点知道要挂起，state 已就位），崩溃是不可预期的中断（节点可能执行到一半，需重跑才能恢复一致）。两种场景的恢复成本不同，不应强求统一。

未执行任务的持久化策略是**不落盘调度队列、恢复时从版本链 + PENDING deliver 重新派生**——调度决策是持久化数据的纯函数，无需额外存储"待执行队列"。这要求触发门逻辑是确定性的（ON_ALL_PREDS 的 BFS 可达性 + ON_RECEIVE 的 deliver 扫描），当前设计满足。

## 13. 实施 ticket 索引

| Ticket | 内容 | 依赖 |
|--------|------|------|
| `issues/34-graph-run-control.md` | `GraphRunControl` + `GraphDrained` 激活 + 两 scheduler 安全点 + 唤醒戳 | — |
| `issues/35-live-engine-controller.md` | `LiveGraphEngineController` + orchestrator `GraphDrained` 映射 + `deliver_to_node` 唤醒 | 34 |
| `issues/36-recovery-entry-derivation.md` | 共享恢复入口集推导 + LinearScheduler 恢复统一 + 时序不变量测试 | — |
| `issues/37-instance-status-semantics.md` | STOPPED 终态化 + `resume` 只认 PAUSED + 状态语义测试 | — |
| `issues/38-sqlite-coordinator-wiring.md` | SQLite `CoordinatorFactory` 装配 + orchestrator 生产接线 + 业务层扫描器示例 | 37 |
| `issues/39-recovery-control-e2e.md` | 暂停-取消-恢复 / 崩溃重叠 deliver / Linear 跳过已完成 / Null 档 fail-safe 的 E2E 测试 | 34, 35, 36, 37 |
