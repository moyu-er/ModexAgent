# Trace/OTel 实现设计缺陷记录

> **RESOLVED 2026-08-18 by `.omo/plans/trace-write-only-refactor.md`** —
> 全部条目已解决(实现 commits `0bbe1525..cc8fb202`;本文件自仓库根目录移入
> `docs/design/otel-collector/DESIGN_GAPS_RESOLVED.md`,作为历史记录与设计文档同存,
> 正文不再更新)。
>
> | 条目 | 状态 | 解决方式 |
> |---|---|---|
> | P0 buffer dict 无界增长(泄漏→OOM) | resolved | write-only store:buffer/LRU 全部删除(`cc8fb202`) |
> | P0 `list_by_trace_id` 持锁全扫描(事件循环冻结) | resolved | 不再读回 spans,指标走增量累积(`7532be59`/`cc8fb202`) |
> | LRU 风险(tool_stats 静默归零 + 640MB–6.4GB 内存) | resolved | write-only store:无 LRU、无 buffer(`e52a2fa9`/`cc8fb202`) |
> | P1 `close()` 生产死代码 | resolved earlier | workspace teardown 接线(`afd48796`) |
> | P1 daemon 线程与 `close()` 语义矛盾 | resolved earlier | sender 线程独占 OTLP client 生命周期(`2ef35f21`) |
> | P2 ScoreInjector 连接不复用 | resolved | 常驻 `AsyncClient` + `ClosableHook` 生命周期(`RootSpanHook.aclose` ← `HookRunner.aclose` ← `AgentPipeline.stop`,`cb01cfb5`) |
> | F1 `inject_scores` 签名不闭合 | resolved | 改吃 `TrajectoryMetrics`,`_build_score_batch(metrics)`(`7532be59`) |
> | F2 root span 累积双重计数 | resolved | per-span-kind 分派:root 及其余 kind 一律 no-op(`0bbe1525`) |
> | F3 eval tool_stats 断裂 | resolved | 读 `TurnCustomKey.TRAJECTORY_METRICS` stash,`source="metrics"`(`e52a2fa9`) |
> | SG1 OTEL_HTTP 读方法行为未定义 | resolved | raise `NotImplementedError("OTEL_HTTP store is write-only; use LangfuseTraceQuery")`(`cc8fb202`) |
> | A1 accumulate 调用位置(guard 前/后) | resolved | store-None guard 之后,off 模式零累积(`7532be59`) |
> | A2 per-span-kind 分派规则未指定 | resolved | CHAT/EXECUTE_TOOL/ITERATION_START 累积,其余 no-op(binding spec)(`0bbe1525`) |
> | Retention gap(文档遗漏) | resolved | 180d ClickHouse TTL(5 表)+ MinIO `events/` ILM + system-log opt-out(`06978bee`) |
>
> 下方正文为 2026-08-17/18 的原始分析记录,保留供追溯。

> 记录于 2026-08-17,针对 `.omo/plans/otel-collector-migration.md` 正在实现的
> `OtelSpanTraceStore` OTel_HTTP 模式(buffer + daemon sender 线程)。
> 以下问题在实现过程中发现,尚未修复,需纳入后续 ticket。
>
> **2026-08-17 修订**:经架构分析,P0 两个问题的修复方向从"反向索引 +
> evict_session"改为"store 回归 write-only + 标量增量累积"。原方案保留在
> 附录供参考,但不再是推荐路径。

---

## 架构修正(核心决策):store 回归 write-only,指标改为增量累积

### 决策

`OtelSpanTraceStore` 的 OTEL_HTTP 模式应**回归纯上报** — 只负责
queue + daemon sender → OTLP POST,**不维护任何读路径 buffer**。

12 个 L2 评估指标的聚合计算从"turn 结束读回全部 spans 重建子树"改为
"每个 span 经过 hook 时增量累积标量计数器"。

### 为什么这是正确的方向

整个 trace 层的本质职责是**上报**,不是数据存储。buffer dict 是为了
让 `RootSpanHook.finally_graph` 读回 spans 算 12 个聚合指标而引入的 —
但这些指标全是**计数/求和/比值**,每个 span 经过 hook 时就已经有全部
所需数据了。读回再算是无用功,且引入了 P0 泄漏 + P0 性能两个致命问题。

### 谁需要读 store buffer?只有一个消费者

| 读方 | 用途 | 是否真的需要读 store |
|---|---|---|
| `RootSpanHook.finally_graph`(`root_span_hook.py:157`) | 读回当前 turn 全部 spans → `compute_root_subtrees` → 算 12 指标 → 注入 Langfuse scores | **不需要** — 指标数据在 hooks 经手时可见,改为增量累积 |
| `experiment_runner` tool_stats | 读 session spans 算 metrics | 不需要 — 12 指标已注入 Langfuse scores,读 scores 即可 |
| `TrainingDataExporter` | 读 spans 导 SFT/DPO | 走 `LangfuseTraceQuery`(Langfuse API),不读 store buffer |

### 新架构

```
span hook → BaseTraceHook._save_span
              ├─ store.save_span(span)           ← 纯上报:queue → OTLP POST(无 buffer)
              └─ session.accumulate_span(span)    ← 增量累积标量(O(1),不存 span)

RootSpanHook.finally_graph
  → session.read_metrics(trace_id)               ← 读攒好的标量(O(1))
  → score_injector.inject_scores(...)            ← 注入 12 scores
  → session.clear_trace(trace_id)                ← 已有清理,自动覆盖累积器
```

### 改动范围

| 文件 | 改动 |
|---|---|
| `session_state.py` | +`accumulate_span(trace_id, root_span_id, span)` 方法 + 标量计数器字段(~15 行);`clear_trace` 加 pop |
| `base_hook.py` | `_save_span` +1 行调用 `session.accumulate_span` |
| `root_span_hook.py` | `finally_graph` 改为读 session 计数器,删 `list_by_trace_id` 调用(~10 行改写) |
| `otel_store.py` | 删 `_session_buffers` / `_buffer_lock` / `list_by_session`(OTEL_HTTP 分支) / `list_by_trace_id`(OTEL_HTTP 分支)(~-40 行) |
| `scoring.py` | `compute_root_subtrees` 在 RootSpanHook 路径不再调用;保留给 exporter/eval(走 LangfuseTraceQuery) |

**净代码减少** — 删的比加的多。

### ⚠️ 资源开销硬性约束(后续必须遵守)

> **累积器的资源占用必须非常轻量。** 这是不可妥协的设计约束。
>
> 累积的是**标量计数器**(int/float 增量),不是 span 对象。12 个指标全部
> 是计数/求和/比值运算,每个 span 经过时只做 `count++` / `+= value` —
> **O(1) 时间,O(1) 空间**,与 ReAct 迭代次数无关。
>
> 一个 trace 的完整累积状态 ≈ 10 个标量 ≈ **80 bytes**。无论 ReAct 跑 5 轮
> 还是 500 轮,累积器大小恒定。
>
> **如果未来需要积累其他数据,必须保持同样轻量**:
> - 累积标量(int/float/bool) — ✅ 允许
> - 累积 span 对象、消息内容、完整 prompt — ❌ 禁止(那就是 buffer,回到 P0)
> - 累积定长聚合(list[float] 用于分位数等) — ⚠️ 需评审,确认定长且有清理
>
> 违反此约束 = 重新引入 P0 泄漏。buffer dict 的教训:**不要在 trace 层
> 存储上报对象本身,上报对象只流经,不驻留。**

### nested subagent root 的处理

累积器按 `(trace_id, root_span_id)` 二级索引,而非只按 `trace_id` —
subagent 的 spans 共享 trace_id 但属于不同的 root span,需隔离避免污染:

```
dict[trace_id, dict[root_span_id, MetricCounters]]
```

- 每个 root 的累积量仍 ~80 bytes
- `clear_trace(trace_id)` pop 整个 trace 的所有 root 桶 — 生命周期自动跟随

---

## 修正后的 P0 问题状态

采用"store 回归 write-only + 标量累积"后:

| 原问题 | 状态 | 原因 |
|---|---|---|
| P0 buffer dict 无界增长(泄漏) | **消失** | 没有 buffer dict 了 |
| P0 `list_by_trace_id` 全扫描(性能) | **消失** | 没有 list_by_trace_id 读回了 |
| 反向索引 `_trace_to_session` | **不需要** | 不读回 = 不需要索引 |
| `evict_session` + `cleanup_session` 接线 | **不需要** | 累积器寄生在已有的 `clear_trace` 生命周期上 |
| 追 `cleanup_session_resources` 调用链 | **不再是阻塞项** | 不依赖 session 级清理钩子 |

---

## P0(原)— per-session buffer dict 无界增长(内存泄漏 → OOM)

> **状态**:已由架构修正消除。以下保留为问题记录,供追溯。

**代码**:`src/modex_agent/trace/otel_store.py:102`

```python
self._session_buffers: dict[str, deque[SpanModel]] = {}
```

### 现状(实现中的缺陷)

- `deque(maxlen=4096)` 封顶了**单 session** 的 span 数,但 `dict` 的 key 集合
  **无上限**。
- 每个 session 一旦写入就永久驻留 — 全代码库无 `pop` / `clear` / `evict`
  调用(已 grep 确认)。
- `TraceSessionState.clear_trace`(`session_state.py:36`,每 turn 结尾在
  `RootSpanHook.finally_graph` 调用)只清自己的 `root_span_info` 等字段,
  **不联动 store 的 buffer dict**。

### 影响量级

| 单条 SpanModel | ~10–100KB(prompt_capture=full 时带完整 system prompt + tool defs) |
|---|---|
| 单 session deque 满载 4096 条 | 40–400MB |
| 100 session × 满载 | **4–40GB → OOM** |

### 根因

buffer 按 `session_id` 组织,但**没有 session 级生命周期清理钩子**。
buffer 的存在本身是设计错误 — 它只为 `RootSpanHook` 读回 spans 算指标而存在,
但指标可以增量累积,不需要读回(见架构修正)。

---

## P0(原)— `list_by_trace_id` 全扫描冻结事件循环

> **状态**:已由架构修正消除。以下保留为问题记录,供追溯。

**代码**:`src/modex_agent/trace/otel_store.py:338-343`

```python
with self._buffer_lock:
    snapshot = [s for buf in self._session_buffers.values() for s in buf]
return sorted(
    (s for s in snapshot if s.trace_id == trace_id),
    key=lambda s: s.start_time,
)
```

### 现状(实现中的缺陷)

- 热路径:每个 `COMPLETED` turn 结尾,`RootSpanHook.finally_graph`
  (`root_span_hook.py:157`)调 `store.list_by_trace_id(trace_id)` 做评分注入。
- 该方法**持 `_buffer_lock`** + 展平**全部 session 的全部 deque** 为一个 list,
  再过滤 trace_id。
- 复杂度 O(N×M),N = session 数(无上限),M = 每 session span 数(≤4096)。
- **持锁期间 `save_span` 被阻塞** — `save_span` 跑在事件循环上,锁冻结 =
  事件循环冻结 = 所有 session 的 turn 停摆。

### 影响量级

| session 数 × spans/session | 持锁时长(估) | 事件循环影响 |
|---|---|---|
| 1 × 50 | <0.1ms | 无感 |
| 10 × 1000 | ~1ms | 轻微 |
| 50 × 2000 | ~10ms | WebSocket/IM 响应延迟可感 |
| 100 × 4096 | **~50–100ms** | **事件循环冻死,所有 turn/save_span 排队** |

### 根因

buffer 主键是 `session_id`,但热路径查询键是 `trace_id` — **索引与查询模式
不匹配**,等价于全表扫描。但更根本的问题是 buffer 不该存在(见架构修正)。

---

## P1 — `close()` 在生产中是死代码(零调用方)

**严重度**:中 — 不直接泄漏(daemon 线程靠进程退出强杀),但 `close()` 方法
形同虚设
**代码**:`src/modex_agent/trace/otel_store.py:263-282`

### 现状

- `close()` 实现完整(stop_event + join + client.close),但全代码库 grep
  `trace_store.close` / `store.close` — **零调用方**。
- `pool_router.py:63` 的注释 `# noqa: B027 - file-backed stores own no resources`
  是旧假设(FILE 模式无资源);OTEL_HTTP 模式打破了这个假设,但 teardown 路径
  未更新。
- 生产中唯一的 pool 销毁路径是进程退出(`resources.py:532` 调
  `shutdown_all`),而 `shutdown_all` 不调 `store.close()`。
- WebUI 删 pool 走 `pool_config_controller.delete_pool` — 只删 config 文件
  + 路由表,**不碰运行中的 AgentPool 实例**(且有 409 守门:有活跃 session
  时拒绝删除)。

### 结论

- **pool 在生产中不销毁** — 实例活到进程退出,daemon 线程靠进程退出强杀,
  httpx.Client 靠 OS 回收 FD。不优雅但**不泄漏**。
- `close()` 目前只有测试会调 — 生产死代码。
- 架构修正后 store 回归 write-only(只剩 queue + client),close 简化为
  stop sender + close client,但仍需接 teardown 才能在生产生效。

### 修复方向(低优先级)

- `AgentPool.shutdown_all` 或 `pool_router.close` 中调 `trace_store.close()`。
- 作废 `pool_router.py:63` 的 `# noqa: B027` 注释。

---

## P1 — daemon 线程与 `close()` 语义矛盾

**严重度**:中 — 与 P1-close 同源
**代码**:`src/modex_agent/trace/otel_store.py:116-121`

### 现状

- sender 线程 `daemon=True` — 主线程退出时被强杀,**不执行清理**。
- `close()` 设计了 `join(timeout=2)` 的优雅退出 — 但 daemon 模式下进程
  退出根本不跑 `close()`。
- 线程退出依赖 `_stop_event` sentinel(L108),`close()` 里 set(L273) —
  逻辑正确,但只在 `close()` 被调用时生效(见 P1-close)。

### 修复方向

与 P1-close 一起解决 — 接上 teardown 调用后,daemon + close 语义自洽。
如需进程退出也优雅,加 `atexit.register(store.close)`(但 daemon 设计本就
接受强杀,非必须)。

---

## P2 — L2ScoreInjector 连接不复用(每 turn 新建 AsyncClient)

**严重度**:低 — 连接抖动/浪费,非泄漏(`async with` 保证释放)
**代码**:`src/modex_agent/trace/score_injector.py:81`

```python
async with httpx.AsyncClient(timeout=5s) as client:
```

### 现状

- 每个 COMPLETED turn 的评分注入都 new 一个 AsyncClient — 新 TCP/TLS 建连。
- `async with` 保证用完即关,不泄漏 FD,但连接不复用 = 每次握手开销。

### 修复方向(可选优化)

改为 store 级持有的常驻 `AsyncClient`(与 Path A 的 `httpx.Client` 对称),
生命周期跟随 store。非阻塞项,可后续优化。

---

## 修复优先级与依赖(修正后)

| 优先级 | 问题 | 修复方式 | 建议 |
|---|---|---|---|
| **P0** | buffer dict 泄漏 + list_by_trace_id 全扫描(同源) | 架构修正:store 回归 write-only + 标量增量累积 | **首要** — 消除两个 P0,净减代码 |
| P1 | `close()` 死代码 | 接 teardown | 架构修正后简化,低优先级 |
| P1 | daemon vs close 矛盾 | 随 P1-close | 同上 |
| P2 | ScoreInjector 连接不复用 | 常驻 AsyncClient | 可选优化 |

---

## 附录:被否决的修复方案(反向索引 + evict_session)

> 此方案在架构修正前提出,现已否决。保留供追溯,不建议采用。

原方案试图在不改变"store 维护 buffer"前提下来修两个 P0:

1. 新增 `dict[trace_id, session_id]` 反向索引,让 `list_by_trace_id` 从
   O(N×M) 全扫描降为 O(M) 单 session 扫描。
2. 新增 `evict_session(session_id)` 方法 pop dict entry,接
   `cleanup_session_resources` 清理钩子。

### 否决理由

- **治标不治本** — buffer dict 仍然存在,只是加了索引和清理;store 仍然
  承担了"数据存储"职责,而非纯上报。
- **evict 触发时机不确定** — `cleanup_session_resources` 的上游调用链
  未确认,如果 bot 运行时 session 无限期驻留不主动 cleanup,evict 永不触发。
- **反向索引引入第二个 dict** — 虽然泄漏面小(100 bytes/条 vs 40-400MB/条),
  但仍是"用一个 dict 的泄漏换一个 dict 的性能",增加了复杂度。
- **架构修正方案更彻底** — 不读回 = 不需要 buffer = 不需要索引 = 不需要
  evict = 两个 P0 直接消失,且净减代码。不修问题,是问题不存在了。

### 反向索引的残留价值

如果未来出现"必须读回 spans"的真实需求(目前没有),反向索引是比全扫描
正确的选择。但当前应优先消除读回需求本身。

---

## Design Closure 分析(2026-08-17,架构修正方案验证)

> 对"store 回归 write-only + 标量增量累积"方案做 design-closure 追踪,
> 验证每条路径是否端到端闭合。以下为发现的新问题。
> 方法:以实现代码为 ground truth,追踪设计提议的路径。

### 维度选择

| 维度 | 选择 | 理由 |
|---|---|---|
| data-flow | ✅ | SpanModel 跨边界(hook→store→queue→OTLP);TrajectoryMetrics 跨接口(session→injector) |
| interface | ✅ | TraceQuery ABC + inject_scores 签名 + _save_span 调用点 |
| lifecycle | ✅ | 累积器 per-trace 生命周期;TraceSessionState per-pool |
| convergence | ✅ | 两条 metrics 计算路径(compute_metrics(spans) vs read_metrics(counters)) |
| state-machine | ❌ | TraceBackend 3 值但无转换动词;trace_id 生命周期简单 create→clear |

### 闭合矩阵

| 维度 | 路径 | 状态 | 备注 |
|---|---|---|---|
| data-flow | span hook → _save_span → store.save_span → queue → OTLP → collector → Langfuse | closed | 实弹验证通过 |
| data-flow | span hook → _save_span → session.accumulate_span → counters | assumption-closed | 假设:accumulate_span 被添加且在 _save_span 中调用 |
| data-flow | RootSpanHook.finally_graph → session.read_metrics → TrajectoryMetrics → inject_scores → Langfuse scores | **gap (F1)** | inject_scores 签名不闭合 |
| data-flow | RootSpanHook._save_span(invoke_agent root) → accumulate_span → counters | **gap (F2)** | root span 累积使用量双重计数 |
| data-flow | TrainingDataExporter → LangfuseTraceQuery → Langfuse API → spans → compute_metrics → SFT/DPO | external | Langfuse API 是终端边界;保真度已实弹验证 |
| data-flow | experiment_runner → store.list_by_session → compute_metrics → ToolStats | **gap (F3)** | OTEL_HTTP 模式下 list_by_session 返回空,tool_stats 归零 |
| interface | inject_scores(trace_id, spans, observation_id) 当前签名 | **gap (F1)** | 设计需要 TrajectoryMetrics,签名未改 |
| interface | _save_span → store.save_span + session.accumulate_span | assumption-closed | 假设:accumulate 在 store-None guard 之后调用 |
| interface | clear_trace → pop 7 dicts + counters | closed | clear_trace 每 turn 已在跑 |
| interface | compute_metrics(spans) 保留给 exporter/eval | closed | 显式延迟 |
| interface | compute_root_subtrees 不再被 RootSpanHook 调用 | closed | 显式延迟给 exporter/eval |
| lifecycle | counters dict[trace_id, dict[root_span_id, MetricCounters]] | closed | clear_trace 每 turn pop,无泄漏 |
| lifecycle | TraceSessionState per-pool | closed | 随 pool 生命周期,per-trace 已 pop |
| lifecycle | OtelSpanTraceStore write-only(无 buffer) | closed | P1-close 是已知延迟项 |
| convergence | 两条 metrics 路径(spans vs counters) | closed | 不同数据源(实时 vs 持久化),同输出类型 TrajectoryMetrics |
| convergence | inject_scores 内部 compute_metrics vs 外部 TrajectoryMetrics | **gap (F1)** | _build_score_batch 也需改 |

### 缝隙检查(Phase 2)

| 缝隙 | 结果 |
|---|---|
| data-flow ↔ lifecycle | closed — score injection 读 counters 在 clear_trace 之前(finally_graph L155→L170 顺序正确) |
| data-flow ↔ interface | **gap (F1)** — counters 产出 TrajectoryMetrics,但 inject_scores 接口仍吃 spans |
| error propagation | assumption-closed — accumulate_span 应与 save_span 一样不 raise(L202 except 兜底) |

### 发现(3 个 gap,均满足 bar:位置 + 后果 + 修复)

#### F1 — `inject_scores` 签名不闭合(接口断裂)

**位置**: `src/modex_agent/trace/score_injector.py:63-69`

```python
async def inject_scores(
    self,
    trace_id: str,
    spans: list[SpanModel],        # ← 当前吃 spans
    *,
    observation_id: str | None = None,
) -> None:
```

内部 `_build_score_batch`(L123-128)也吃 `spans`,在 L136 调
`compute_metrics(spans)` → `TrajectoryMetrics`。

**后果**: 架构修正后 RootSpanHook 读 session 计数器得到 `TrajectoryMetrics`,
但 `inject_scores` 仍期望 `list[SpanModel]`。RootSpanHook 不再有 spans 可传
(buffer 已删除)→ 调用无法闭合 → 12 个 L2 scores 不会被注入 Langfuse →
eval `compare` 命令读不到 scores → eval 飞轮断裂。

设计文档的改动范围表(`TRACE_OTEL_DESIGN_GAPS.md` 架构修正章节)列出了
`session_state.py`/`base_hook.py`/`root_span_hook.py`/`otel_store.py`/
`scoring.py` 五个文件,**遗漏了 `score_injector.py`**。

**修复**:
1. `inject_scores` 签名改为 `inject_scores(trace_id, metrics: TrajectoryMetrics, *, observation_id)`。
2. `_build_score_batch` 签名改为接受 `TrajectoryMetrics` 而非 `spans`,删除内部的 `compute_metrics(spans)` 调用。
3. 改动范围表增加 `score_injector.py`。

#### F2 — root span 累积使用量双重计数(正确性缺陷)

**位置**: `src/modex_agent/trace/root_span_hook.py:140-163`

`finally_graph` 序列:
1. L140: `await self._save_span(...)` — 保存 invoke_agent root span,其 attributes
   包含从 `session.turn_usage`(L118-133)设置的**累积使用量**(整 turn 的 token 总和)。
2. 如果 `_save_span` 调用 `accumulate_span`,root span 的累积使用量被加到 counters。
3. L155-163: 读 counters → 注入 scores → **scores 中的 token 字段双重计数**。

`compute_metrics`(scoring.py:37-39)明确避免此问题:"Token usage is summed
from chat spans only — NOT from the invoke_agent root span, which carries
cumulative usage that would double-count"。它通过 `chat_spans = [s for s in
spans if s.name == CHAT]` 过滤实现。但 `accumulate_span` 如果不加同样的过滤,
就会把 root span 的累积 token 加到 chat-span-only 的 sum 上。

**后果**: `total_input_tokens` / `total_output_tokens` / `total_reasoning_tokens`
变为正确值的 2 倍(每 chat span 的 token 被计一次 + root span 的累积值再计一次)。
`cache_hit_rate` / `response_token_ratio` 等派生指标也随之失真。Langfuse 中的
12 scores 全部 token 相关字段错误 → eval 排序/筛选基于错误数据。

**修复**: `accumulate_span` 按 span name 分派,只处理:
- `CHAT` span → 累积 usage(input/output/reasoning/cache_read tokens)+ latency sum + llm_count++
- `EXECUTE_TOOL` span → tool_count++ + error_count++(if status==ERROR)
- `ITERATION_START` span → iteration_count++
- 其他 span kind(invoke_agent / handoff / approval / agent_start / iteration_end)
  → **no-op,不累积任何字段**

设计文档应明确此分派规则。

#### F3 — `experiment_runner` tool_stats 在 OTEL_HTTP 模式下断裂(功能回归)

**位置**: `examples/bot_project/bot/eval/experiment_runner.py:166-177`

```python
async def _span_tool_stats(
    store: OtelSpanTraceStore | None,
    session: SessionInfo,
) -> ToolStats:
    spans = await store.list_by_session(str(session)) if store is not None else []
    metrics = compute_metrics(spans)
    return ToolStats(
        total=metrics.tool_call_count,
        errors=metrics.error_tool_count,
        success_rate=metrics.tool_success_rate,
        source="spans",
    )
```

**后果**: 架构修正删除 `list_by_session` 的 OTEL_HTTP 分支 → 该方法在
OTEL_HTTP 模式下返回 `[]`(或 raise)→ `compute_metrics([])` 返回全零 →
`ToolStats(total=0, errors=0, success_rate=1.0, source="spans")`。每个实验项
的 tool_stats 恒为零/1.0,eval 实验结果失真。设计文档说"不需要 — 12 指标
已注入 Langfuse scores,读 scores 即可",但 experiment_runner 是**同步实验
流程**(turn 结束后立即需要 stats),读 Langfuse scores 有 ~12s 异步延迟
(events_only consistency),在实验循环中不可靠。

**修复**: RootSpanHook 在 `clear_trace` 之前将 `TrajectoryMetrics`(或至少
tool_count/error_count/success_rate 三个字段)stash 到 `ctx.runtime.state.custom`
或 `AgentResult.metadata`。`_span_tool_stats` 改为从 turn 结果读取,不再调
`store.list_by_session`。无网络、无延迟、无新存储。

### 疑似缺口(1 个,后果不完全明确)

#### SG1 — `list_by_session`/`list_by_trace_id` OTEL_HTTP 分支删除后的方法行为未定义

**位置**: 设计文档"改动范围"表: "删 `list_by_session`(OTEL_HTTP 分支) /
`list_by_trace_id`(OTEL_HTTP 分支)"

**追踪**: 当前 `list_by_session` 有 `if backend == FILE: [读 jsonl]; with
buffer_lock: [读 buffer]`。删除 OTEL_HTTP 分支后,OTEL_HTTP 模式下方法落到
无返回逻辑 — 是返回 `[]`?raise `NotImplementedError`?还是从 `TraceQuery`
ABC 中移除方法?设计未指定。

**缺失**: OTEL_HTTP 模式下 `list_by_session`/`list_by_trace_id` 的契约。
如果返回 `[]`,F3 的 tool_stats 静默归零(不报错,更危险)。如果 raise,
experiment_runner 会崩(更早暴露)。如果从 ABC 移除,所有实现都要改。

**为什么后果不明确**: 取决于是否有 F3 之外的其他调用方。已知调用方:
RootSpanHook(F1 修复后不再调)、experiment_runner(F3)、tests。如果 F1+F3
修复后无生产调用方,方法可安全删除(返回 `[]` 或 raise 均可,仅供测试)。
但设计应明确选择哪种,避免实现时随意处理。

### 歧义(2 个)

#### A1 — `accumulate_span` 在 `_save_span` 中的调用位置

**位置**: 设计文档"改动范围": "`base_hook.py`: `_save_span` +1 行调用
`session.accumulate_span`"

**解释**: `_save_span`(`base_hook.py:187`)有 `if self._store is None: return`
guard。accumulate 在 guard 之前还是之后?
- 之前: trace_backend=off 时也累积(浪费但无害)
- 之后: 只在 trace 开启时累积(正确)

**追踪影响**: 不闭合 — 如果之前,off 模式下 counters 有数据但无 score 注入
(RootSpanHook L155 检查 `self._store is not None`),counters 泄漏(clear_trace
仍 pop,但中间状态不一致)。应在 guard 之后调用。

#### A2 — `accumulate_span` 的 per-span-kind 分派规则未指定

**位置**: 设计文档"架构修正"章节的累积器表列出了 12 个指标,但未指定
`accumulate_span` 如何按 span kind 分派。

**解释**: 12 个指标需要从不同 span kind 提取不同字段:
- CHAT → usage tokens + latency + llm_count
- EXECUTE_TOOL → tool_count + error_count
- ITERATION_START → iteration_count
- 其他 → no-op(F2 要求)

设计只说"accumulate_span(trace_id, root_span_id, span)"泛化签名,未列分派表。

**追踪影响**: 如果实现者不知道分派规则,可能对所有 span 统一提取 usage →
触发 F2 的双重计数。应显式列出分派规则。

### 验证完成状态

| 完成标准 | 状态 |
|---|---|
| 每个 map 项出现在 ≥1 矩阵行 | ✅ |
| 每行有状态(无空) | ✅ |
| 每个 gap 行有满足 bar 的 finding | ✅ F1/F2/F3 均满足 |
| 每个 assumption-closed 行有假设记录 | ✅ |
| 跨维度缝隙已追踪 | ✅ 3 个缝隙检查 |
| 每个 finding 已重新验证(Phase 3) | ✅ 逐条回读代码确认 |

---

## 代码核对结果(2026-08-18,核对当前实现状态)

> 用户指示:核对当前代码,看 F1-F3/SG1/A1/A2 是否在实现中已处理。
> 结论:**架构修正方案完全未落地**,当前代码走的是原版规划 + LRU 缓解。
> F1-F3/SG1/A1/A2 是架构修正方案的待修项,当前实现不触发它们(因为
> accumulate_span/inject_scores 签名变更等都没做)。但原版规划的 P0
> 两个问题被 LRU **部分缓解**,且 LRU 引入了一个新风险。

### 当前实现状态(原版规划 + LRU)

| 组件 | 状态 | 代码位置 |
|---|---|---|
| `_session_buffers` | `OrderedDict` + `max_buffered_sessions=16` LRU | `otel_store.py:110, 97` |
| LRU 驱逐逻辑 | 写新 session 时超 16 → `popitem(last=False)` 驱逐最旧 | `otel_store.py:209-210` |
| `list_by_trace_id` OTEL_HTTP | 仍全扫描 `values()`,但 N ≤ 16;**持锁只做 snapshot 拷贝,过滤排序无锁** | `otel_store.py:410-414` |
| `list_by_session` OTEL_HTTP | 返回 LRU buffer 快照;**LRU 驱逐的 session 返回 []** | `otel_store.py:369-372` |
| `accumulate_span` | **不存在** — `session_state.py` 仍是 44 行原始版本 | `session_state.py` |
| `inject_scores` 签名 | **未改** — 仍吃 `spans: list[SpanModel]` | `score_injector.py:63-66` |
| `experiment_runner._span_tool_stats` | **未改** — 仍调 `store.list_by_session` | `experiment_runner.py:170` |
| `close()` | 改进了 sender finally 关 client;但仍是 daemon + 零生产调用方 | `otel_store.py:242-257, 316-343` |

### P0 泄漏(buffer dict 无界增长)— 已由 LRU 缓解

**原问题**:dict key 集无上限 → OOM。
**当前**:LRU `max_buffered_sessions=16` 硬限 dict 大小 → 不再 OOM。
**残余**:
- LRU 驱逐丢数据 — 被驱逐 session 的 buffer 消失,`list_by_session` 返回 `[]`
- 16 sessions × 4096 spans × 10-100KB = **640MB–6.4GB 最坏内存** — 仍偏高但不会 OOM
- `clear_trace` 仍不联动 buffer(每 turn pop TraceSessionState,但不 pop store buffer)

### P0 性能(list_by_trace_id 全扫描)— 已部分缓解

**原问题**:O(N×M) 持锁全扫描,N 无上限 → 事件循环冻死。
**当前**:
- LRU 限制 N ≤ 16 → 最坏 16 × 4096 = 65536 spans
- **持锁只做 snapshot 拷贝**(L410-411),过滤+排序无锁(L412-414)— 锁粒度缩小
- 最坏持锁 ~5-10ms(65536 引用拷贝)— 可感但不冻死
**残余**:16 个活跃 session 仍全扫描,无反向索引

### 新发现:LRU 驱逐导致 experiment_runner tool_stats 静默归零

**位置**: `experiment_runner.py:170` + `otel_store.py:369-372`

`_span_tool_stats` 调 `store.list_by_session(str(session))`。OTEL_HTTP 模式下
该方法返回 LRU buffer 快照。**如果实验期间有 >16 个 session 写入**,正在实验
的 session 可能被 LRU 驱逐 → `list_by_session` 返回 `[]` → `compute_metrics([])`
返回全零 → `ToolStats(total=0, errors=0, success_rate=1.0, source="spans")`。

**后果**:eval 实验的 tool_stats 静默归零(不报错),实验结果失真。比架构修正
方案的 F3 更隐蔽 — F3 是"方法返回空"的确定性断裂,这个是"LRU 偶然驱逐"的
概率性断裂,更难排查。

**触发条件**:eval 并发跑 >16 个 session,或 bot 运行中有 >16 个活跃 session
时跑 eval。单 session 串行 eval 不触发。

### F1-F3/SG1/A1/A2 状态(架构修正方案待修项)

| 编号 | 描述 | 当前代码状态 |
|---|---|---|
| F1 | `inject_scores` 签名吃 spans | **未触发** — 当前实现 RootSpanHook 仍调 `list_by_trace_id` 读 spans 传给 injector,签名未变 |
| F2 | root span 累积双重计数 | **未触发** — `accumulate_span` 不存在,仍走 `compute_metrics(spans)` 的 chat-only 过滤 |
| F3 | experiment_runner tool_stats 断裂 | **当前实现有等价问题** — 见上"新发现"(LRU 驱遏导致归零) |
| SG1 | list_by_session OTEL_HTTP 删除后行为 | **不适用** — 当前实现保留了 OTEL_HTTP 分支,返回 LRU buffer 快照 |
| A1 | accumulate 在 guard 之前/之后 | **不适用** — accumulate 不存在 |
| A2 | 分派规则未指定 | **不适用** — accumulate 不存在 |

**结论**:F1/F2/SG1/A1/A2 是"如果做架构修正,需要处理的待修项"。当前实现
不触发它们(走原版规划路径)。F3 的等价问题(LRU 驱逐归零)在当前实现中
**真实存在**,是原版规划 + LRU 的固有缺陷。

### 当前实现的残余问题清单(更新)

| 优先级 | 问题 | 状态 | 触发条件 |
|---|---|---|---|
| P0-残余 | LRU 驱逐导致 experiment_runner tool_stats 静默归零 | **真实存在** | eval 期间 >16 session |
| P1-缓解 | buffer dict 内存(16×4096×10-100KB = 640MB-6.4GB) | 偏高但不 OOM | 16 活跃 session 满载 |
| P1-缓解 | list_by_trace_id 全扫描(16×4096 持锁 ~5-10ms) | 可感但不冻死 | 16 活跃 session |
| P1 | close() 生产死代码 | 未变 | daemon + 零调用方 |
| P2 | ScoreInjector 连接不复用 | 未变 | 每 turn new AsyncClient |
| 待修(架构修正) | F1/F2/SG1/A1/A2 | 未触发 | 仅在切换到架构修正方案时 |

### 建议

当前实现(原版规划 + LRU)在 **≤16 活跃 session** 的场景下可工作,但有两个
真实风险:
1. LRU 驱逐导致 eval tool_stats 静默归零(概率性,难排查)
2. 16 session 满载时内存偏高(640MB-6.4GB)

如果继续用原版规划,至少需要:
- `experiment_runner._span_tool_stats` 改为不依赖 `list_by_session`(从 turn
  结果读 tool_stats,或 RootSpanHook stash metrics 到 turn state)
- 考虑调大 `max_buffered_sessions` 或降低 `buffer_maxlen` 控制内存

如果切换到架构修正方案(store write-only + 标量累积),F1/F2/SG1/A1/A2 全部
需要处理(见 design-closure 章节),但 P0 两个问题和 LRU 驱逐风险全部消失。
