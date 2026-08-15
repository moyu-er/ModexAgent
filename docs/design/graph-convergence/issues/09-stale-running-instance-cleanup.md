# 09 — 脏 RUNNING 实例清理定稿与落地

Status: open (设计定稿,待实施 — 见 Comments)
Labels: wayfinder:deliberate
Assignee: GYT
Blocked-by: 03 (实施链 05→07→03→09)

## Question

进程被杀后 graph instance 停留 `RUNNING`,当前唯一回收路径是启动时 `GraphRecoveryService.recover_crashed()` 拾取(CRASHED + 孤儿 RUNNING)。缺口:

1. **进程内视角**:`GraphOrchestrator._running_gids` 与 `GraphInstanceStore` 状态可能失协 — 任务被取消/异常路径未走 `finally` 清理(理论上 finally 覆盖,验证是否有漏洞,如 `finalize_instance` 自身抛错)、或 store 状态被外部改写后与活体执行冲突。
2. **同 DB 多进程/重启间隙**:无心跳/超时机制区分"真在跑"与"僵尸 RUNNING"。建议文档 P2 提 lease/heartbeat — 用户已将其列入本轮范围,但需定稿**最小形态**:是否只需 (a) 启动时孤儿拾取(现状,补验证与测试)即可,还是需要 (b) orchestrator 内存态与 store 的周期性对账,或 (c) 完整 heartbeat 租约。收敛优先,从 (a) 开始证。

产出:定稿清理策略等级 + 实现 + 测试(含 01 票审计中实例层窗口的守护用例)。

## Comments

**决议(2026-08-15,与 GYT deliberate 定稿):进程号归属 + 状态化清扫,扩展不局限单进程。**

**设计**(用户方案 + 分层修正):
1. **框架只提供存储缝,业务控制字段语义**:GraphMetadata 增通用 `attrs: dict[str, int|str|None]`(node_id_map 同款先例;框架永不解释);GraphInstanceStore 增 `update_attrs(gid, attrs)` 最小写缝(Sqlite attrs_json 可空列纯增量迁移;Null no-op);版本间随 node_id_map 复制。modex_graph 全程无"进程"概念。
2. **modex_agent 层**:`ProcessIdentity`(进程级 snowflake,惰性生成,重启=新号;**生成时 INFO 日志:进程号+主机名+OS PID**)+ `ProcessRegistry` ABC:`alive_process_ids() → set[int]`,单例实现返回 `{自己}`(单元素集合,零基建,即多实例扩展点)。GraphOrchestrator 可选注入 ProcessIdentity,注入则在 begin_invocation/恢复置 RUNNING 时写 executor_process_id 进 attrs。
3. **bot_project 业务层**:定时任务扫 RUNNING 实例,executor_process_id ∉ alive_process_ids() → 刷 CRASHED(状态化处理脏内容)。多实例未来=换 Registry 实现,判定与清扫零改动。
4. **recover_crashed 增强**:拾取 RUNNING 时 executor ∉ alive → 先刷 CRASHED → 走既有 CRASHED 恢复路径;executor 属活集合 → 正常恢复。
5. **存量 executor=NULL** ∉ 任何活集合 → 天然视为脏,无需特判。
6. **清扫只刷状态,不触发恢复**(与 08 意图显式化同哲学:清扫=事实修复,恢复=显式意图)。

**明确不做(防误导)**:lease/heartbeat/续约;进程表持久化(Registry 接口即扩展点);自动 failover(清扫只认死,接管必须显式);跨库/网络进程发现。

**实施结构定案(2026-08-15)**:05 → 07(含 08 的 bootstrap mode/END/reached_end 改造)→ 03 → 09(本票:attrs 缝+Identity/Registry+清扫任务,独立小实现票)→ 12(矩阵)→ 13(ADR 收口)。

**评审修订(2026-08-15)**:①终态/历史版本的 executor_process_id **保留为审计痕迹**,清扫只扫 RUNNING,不清理;②attrs 的 `dict[str, int|str|None]` 是对类型规则的**已文档化例外**(扩展边界,node_id_map 先例),业务侧(modex_agent)用集中 typed key 常量收口,不得散落字符串字面量;③ProcessRegistry ABC 单实现属用户显式指定的扩展缝(架构规则 6 例外,记录在案);④验收清单:attrs 三实现一致(Sqlite 列迁移+版本复制/InMemory/Null no-op)、Identity 日志(号+主机+PID)、Registry 单例返回、NULL 视为脏、清扫只刷状态不触发恢复、终态不动、bot 端到端(杀进程→清扫→CRASHED→恢复)。
