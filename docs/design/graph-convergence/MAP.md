# Wayfinder Map: 图调度收敛 — node/graphInstance/deliver 三合一机制的正确性收敛与验证

Status: open
Labels: wayfinder:map

## Destination

图调度子系统收敛完成并可验证:ReAct 数据流 deliver 化(不再依赖共享 state 传递)、目标侧 STAGED 四态投递落地(复用既有 DeliverStore ABC,零新持久化族)、崩溃重试重复=文档化 at-least-once(不关闭)、全局 state 快照层整体退役(三合一恢复模型:重执行+deliver 准入+版本链)、bootstrap 意图显式化(FRESH/RECOVERY)、进程归属清扫落地,且 node/graphInstance/deliver 三合一机制的可恢复/可中断/幂等不变式由崩溃窗口矩阵测试守护 — 所有决策已定稿、代码已落地、测试全绿、ADR 已合并更新。

## Notes

- **本地图携带执行**(用户决策):每张票先解决决策,随后同票落地实现+测试,测试绿后才可关闭。不是纯规划地图。
- 领域: `src/modex_graph/`(独立图引擎,禁 import modex_agent)、`src/modex_agent/orchestration|control/`(编排/恢复/控制面)、`src/modex_agent/agents/react/`(ReAct 图)、`examples/bot_project/bot/graph/`(业务接入)。
- 必读参考(按权威度排序):
  - `src/modex_graph/AGENTS.md` — 调度收敛设计(bootstrap/版本链/deliver 准入)
  - `docs/adr/0033-generalized-graph-engine.md`、`0034-parallel-scheduling-engine.md`、`0040-graph-instance-re-invocation-and-iorecord-version-scoping.md`
  - `docs/handoff/suggestion.md` — 外部评审建议,**参考而非唯一事实源**;其 P0.1(一致性提交)按"prove-or-fix"处理
  - `docs/design/graph-orchestration/distributed-persistence.md` — 持久化权威描述
- 仓库规则:收敛规则(root AGENTS.md: 不加第三条路径、不为新代码留兼容 shim)、类型安全/架构 rules、ADR 活文档治理(细化合并入原 ADR,不开平行版本)。
- 验证标准:**崩溃窗口矩阵测试** — 枚举每个可注入崩溃点,断言恢复不变式(可恢复/不丢失/不重复/幂等)。property-based 随机序列测试不在本轮范围。
- 每张票关闭时:在票文件底部 `## Comments` 追加决议,并把一行 gist 追加到本地图 Decisions so far。
- claim 票 = 在票文件 `Assignee:` 行写 `GYT`。
- Blocking 用票文件内 `Blocked-by: NN` 行表达(本地 markdown 无原生依赖关系)。frontier = open、无未决 Blocked-by、无 Assignee 的票。

## Decisions so far

- [01 崩溃窗口与不变式审计](issues/01-crash-window-invariant-audit.md) — 14 个崩溃窗口(W1-W14)全部定位;8 个 DEFECT:D1 输出重复 deliver(P0→06/04c)、D2 re-invoke 版本间 deliver 泄漏(P0→08)、D3 并发快照陈旧 scratch(P1 潜伏→07)、D4 execute→submit 重执行代价(P1→04b)、D5 ADR-0038 文档-代码偏差(P1→13)、D6 stop 静默 CAS(P2→09)、D7 finalize/IORecord 窗口(P2→12)、D8 LINEAR 准入缺口(P3→10);输入侧 at-least-once(W3)、auto-promote(W4)、孤儿清理(W5)等 6 窗口确认为 OK-BY-DESIGN;10 个无覆盖测试缺口供 12 票。详见 [research/crash-window-audit.md](research/crash-window-audit.md)。
- [02 ReAct 数据流 deliver 化设计](issues/02-react-deliver-ization-design.md) — 数据通道三分:history 唯一持久上下文源、deliver 纯路由+瞬态单跳数据(默认 payload=None,仅错误描述符带最小 payload)、state 只存 turn 生命周期;删除 `state.llm_response`,接收节点改读 history 末条/integrated_input;错误路径 `phase=FAILED`+`deliver({"error": text})` 并入 FAILED 分支;ReAct 维持 NullDeliverStore 内存语义;approval 挂起恢复机制不动;无框架级 codec。与 bot 图 AgentNode 模式同构(三合一收敛)。
- [04 PendingDeliverStore 语义定稿](issues/04-pending-deliver-store-semantics.md) — 目标侧 STAGED 投递,零新 ABC:deliver() 在 execute 内即刻落库到目标侧 DeliverStore(status=STAGED 下游不可见),复用 Null/InMemory/Sqlite 三实现;四态状态机;**绝不做废**(deliver=不可撤销产出声明,作废是业务节点可选能力 void_staged,框架零调用),complete 后按 source_node_id 全量刷新(含历史崩溃尝试残留)→PENDING,bootstrap 按来源节点对称 auto-promote 兜底微秒间隙;崩溃重试重复投递=by-design at-least-once(与既有测试断言一致);D4 修正为输出不再丢失;SQL 无需新列(source_node_id/source_invocation_id 已有);ReAct 装 Null(deliver 即可见)、bot 图装 SQLite;06 缩编维持(框架级去重键明确拒绝)。05 票已按此重写。
- [06 Deliver 幂等键(缩编后关闭)](issues/06-deliver-idempotency-key.md) — 框架级去重键被 04 决议拒绝(绝不作废+按来源提升=at-least-once by design);外部投递调用方重试重复同样接受 at-least-once,文档化并入 13;纵深防御不做(两用例原则)。未开实现,零代码遗留。
- [08 re-invocation 状态语义与 deliver 账本](issues/08-reinvocation-state-semantics.md) — 意图显式化定稿:bootstrap 增 mode(FRESH=[entry] 零扫描/RECOVERY=完整推导),第 5 步意图猜谜退役;账本不动(Q2 撤回:残留可见=正确设计,PENDING 不丢弃/CONSUMED_PENDING 业务过滤/STAGED 跨版本提升);种子推导微调=去 END 跳过(三扫描+BFS+_can_reach_active),START 保持跳过(空种子回退已覆盖);空种子→[entry](START-only 崩溃);reached_end 于 END 执行即置位;auto-promote 双补全前置于种子推导;状态继承问题随 07 退役消失。
- [10 DeliverStore 扫描收敛(关闭,零实现)](issues/10-deliver-scan-convergence.md) — 设计收敛由 05/08 完成(dispatch=纯唤醒/store=唯一数据面,双路径即设计意图);扫描合并/共享快照/增量失效层**明确不做**(≈5 节点图微秒级查询非热点);重估门槛:百节点级图或 store 网络化。
- [11 版本链与 spec GC(关闭,零实现)](issues/11-version-chain-and-spec-gc.md) — 两项**明确不做**:版本链优化(版本=重试次数个位量级,07 删列后负载结构性下降;门槛:单链百级);spec GC(累积=人工编辑人类尺度,错删代价>存储;门槛:千行或对外发布前)。ADR-0040 deferred 补记归 13。
- [09 脏 RUNNING 实例清理(设计定稿,待实施)](issues/09-stale-running-instance-cleanup.md) — 进程号归属+状态化清扫:GraphMetadata 增通用 attrs 扩展位(node_id_map 先例,框架只提供存储缝永不解释)+update_attrs 写缝;modex_agent 提供 ProcessIdentity(生成时 INFO 日志)+ProcessRegistry ABC(单例=返回自己,即多实例扩展点);orchestrator 可选注入写 executor_process_id;bot 定时清扫 executor∉alive 的 RUNNING→CRASHED(只刷状态不触发恢复);NULL 视为脏;明确不做 lease/heartbeat/进程表/failover。实施结构定案:05→07(含08)→03→09→12→13。
- 05 deliberate 完成(2026-08-15,票未关待实施):入口分离(节点 deliver→STAGED/外部 deliver_to_node→直写 PENDING)、promote 返回受影响目标集驱动 dispatch 寻址(内存累加彻底删除)、InMemory 认领语义与 SQLite 统一为四态机(修审计外分歧:恢复测试曾测错语义)、bootstrap STAGED auto-promote 必须前置于 seed 推导(否则 99% 完成的图被从头重跑)、promote 失败 raise 不吞错、CONSUMED 枚举移除、(gid,source_node_id,status) 部分索引、UndeliveredError 检测点后移;IntegratedPayload 增 status+consumed_by(业务按状态 DIY integrate,AgentNode 收敛为状态过滤样板,自建 collect 路径删除);实施两阶段(store 层先行/机制重排随后)。
- 07 deliberate 完成(2026-08-15,票未关待实施):**全局 state 持久化层整体退役** — 核实零消费者(ReAct 审批走 agent 层 TurnSnapshot、bot 图不读、调度/恢复只读 status+deliver);删 complete 的 state 参数/suspend 快照/Node.run resume 分支/rebuild_main_state/suspended 与 state_json 列/bootstrap suspended-seed;挂起=interrupt→节点 CANCELED+实例 PAUSED,恢复=deliver 到达→重新执行;崩溃/挂起/外部三触发收敛为唯一恢复模型(重执行+deliver 准入+版本链);W7 结构性消失;HumanInputNode 改幂等模式;实施排 05 后。
- 全量设计审核(2026-08-15,三 oracle 评审:一致性/闭环/冗余):终态机制合成通过;20 处文档修正(WebUI graph_routes state_json 迁移入 07、bootstrap 模式权威表、CANCELED 尝试级/实例级规范定义、12 票 Blocked-by+7 矩阵行+D6/D7/D8 接受+文档化、13 票 7 项文档义务、输出基数口径全票统一、旧文本 supersede 标记、审计文档历史基线 banner);决策落定:HITL 恢复=显式 resume only(答案 deliver 不自动恢复)、void_staged 移除(业务 exactly-once 走 integrator 过滤)、**GraphAsNode 删除**(零生产使用,子图组合=节点实现自由,内层不参与外层生命周期,嵌套寻址难题消解,ADR-0033 D8 移除归 13)。设计地图审核后定稿,进入实施(05 起步)。

## Not yet specified

- **循环图的 join 语义精细化**:若未来单调度器合并重启或 ReAct 图跑 ParallelScheduler,`ON_ALL_PREDS` 的静态可达性闭包在循环拓扑下可能过度等待,需 epoch/round 或 activation token(建议文档 P1.5)。当前双轨保留后暂不触发,等本次收敛落地后重估。
- **ON_RECEIVE 触发器的最终移除**:已废弃但仍在代码中;待确认无任何使用者后删除(并入调度器准入收敛或届时单独立票)。

(fog 已清两项:快照放大治理 — 随 07 快照层物理删除而消失;deliver 类型化 codec — 02 已定无框架 codec,触发条件=第三个强类型 payload 消费者,记录于 02 票。)

## Out of scope

- **删除 LinearScheduler 合并单调度器** — 用户已决策:双调度器保留,ReAct 数据流 deliver 化即可(方案 3)。
- **NodeRecoveryPolicy 显式契约建模**(建议文档 P0.3:replay_mode/side_effect_mode/ctx.is_reexecution)— 本轮不做;框架事实注入随各票按需最小化。
- **Property-based 随机崩溃/调度序列测试** — 验证标准已定为崩溃窗口矩阵,随机序列留待后续。
- **资源治理/背压/分布式**(建议文档 P1.4、P2:max_concurrency、lease/heartbeat 多进程、PG 后端、四级因果 trace等)— 全部超出"收敛+正确性"的终点范围。
