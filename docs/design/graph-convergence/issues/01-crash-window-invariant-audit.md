# 01 — 崩溃窗口与不变式审计:三合一机制的完整窗口清单

Status: closed
Labels: wayfinder:research
Assignee: GYT
Blocked-by: (无 — frontier)

## Question

在当前代码(Node.run 生命周期、bootstrap、dispatch/route_deliver、GraphOrchestrator.run_instance、控制面 pause/resume/stop/deliver_to_node)中,**穷举每一个可注入的崩溃点**(两次持久化写入之间的每个交错点),并对每个窗口回答:

1. **窗口在哪**:精确到代码路径与写入顺序(如 `route_deliver 落库 → complete_invocation` 之间)。
2. **当前恢复行为是什么**:崩溃后 bootstrap/重执行/孤儿清理各如何处置该窗口。
3. **违反了哪条不变式**(可恢复 / 输入不丢失 / 输出不重复 / 幂等):明确标注"at-least-once by design(可接受)"与"真实缺陷"的区别。
4. **现有测试覆盖了吗**:在 `tests/` 与 `examples/bot_project/tests/` 中定位对应测试;未覆盖的窗口列表。

产出写入 `docs/design/graph-convergence/research/crash-window-audit.md`,作为后续所有票(04 pending_deliver 语义、05 幂等键、11 崩溃矩阵)的**事实基础**。

特别要求验证建议文档(docs/handoff/suggestion.md §六)声称的四个边界在当前代码中是否属实,尤其是"输出侧重复 deliver 窗口"的精确触发条件与影响面 — 但以源码为准,不以文档断言为准。

## Comments

Audit complete — see `docs/design/graph-convergence/research/crash-window-audit.md` (14 windows W1-W14, all with file:line evidence).

Hypothesis verdicts: (1) output duplicate-deliver window CONFIRMED — route_deliver persists inside submit, strictly before complete_invocation (node.py:237/240/444, _dispatch_utils.py:76, deliver_store.py:450); retry mints new Snowflake ids, no idempotency key. (2) input at-least-once CONFIRMED — bootstrap auto-promote gates on COMPLETED consumer (bootstrap.py:98-102); AgentNode always-filters CONSUMED_PENDING (agent_node.py:115-118); ADR-0038 D5's BotAgentNode re-execution detection claim is stale vs source (agent_node.py:216-218 re-injects Origin Request unconditionally). (3) concurrent snapshot cut: mechanism CONFIRMED (scratch never reset, readable), real-graph harm REFUTED (no shipped node reads own scratch; ReAct is LINEAR; only review_cycle.yml is parallel). (4) orchestrator windows verified OK (orphan RUNNING absorbed by recover_crashed, graph_recovery.py:115-117) with silent-CAS caveat on instance store. (5) re-invocation deliver leak CONFIRMED both paths — v1 PENDING hijacks v2 seeds and suppresses the entry_node branch (bootstrap.py:77-85/115-126); CONSUMED_PENDING re-consumed by plain nodes. (6) external deliver: no loss; LINEAR has no in-run admission.

suggestion.md §六: claims 1/2/4 TRUE; claim 3 mechanism TRUE, current harm FALSE.

DEFECT count: 8 (D1 duplicate delivers [P0 → 06/04c], D2 re-invocation leak [P0 → 08], D3 concurrent snapshot latent [P1 → 07], D4 execute→submit re-execution cost [P1 → 04b], D5 ADR-0038 doc-code divergence [P1], D6 stop/silent-CAS [P2 → 09], D7 finalize/IORecord window [P2], D8 LINEAR admission gap [P3 → 10]). 10 uncovered-window test gaps enumerated for ticket 12.

