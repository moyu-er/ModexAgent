# ADR-0016: ReAct 循环检测作为受控退出

- **Status:** Accepted
- **Date:** 2026-07-07
- **Revised:** 2026-07-10 — 检测语义由「内容循环 OR 工具循环」改为「内容相似 AND 工具相同」的合取判定（见 §4）。原 OR 方案对正当的重复步骤（多步确认、迭代探查）误报过多；合取要求两个信号在同一段连续窗口上同时成立才介入。
- **Revised:** 2026-08-28 — 检测从「`after_llm_response` 合取判定（内容相似 AND 工具相同）、命中即硬断」改为「`before_iteration` 两阶段守护：窗口提醒（system-reminder）→ 观察期 → 受控退出」。内容相似度机制整体移除（误杀正当重复、且对大周期循环无效）；退出时面向用户的内容从 XML 模板改为纯文本。参考实现：deepseek-harness `dsh-repeat-tool-reminder`（advisory-only 守卫，其 README 明确将「升级为阻断式」列为 deferred —— 本设计的第二阶段即该 deferred 项）。详见 §4。
- **Related:** ADR-0008（审批主 agent 镜像与 sanitizer）、`AgentControlError` 退出模型（`src/modex_agent/control/exceptions.py`）、`ToolCallDeduplicator`（`src/modex_agent/agents/react/tool_dedup.py`）

## Context

### 问题

某些模型在 ReAct 循环中会陷入两类死循环，空耗 token、永不结束 turn：

1. **内容循环**：连续多次 assistant 输出高度相似甚至相同的纯文本回复（无工具调用）。
2. **工具循环**：连续多次调用**同名 + 同参数**的工具（例如反复 `read` 同一个 path、反复 `ls` 同一目录）。

现有机制无法检测：`max_iterations` 只数迭代次数，不关心内容是否重复；已有的 todo 相关机制只在“有未完成 todo 且想结束 turn”时介入，与死循环无关。

### 现有架构契合点

项目已有一套统一的“受控退出”模型：

- `AgentControlError` 是 `AgentCancelledError` / `AgentTimeoutError` / `PolicyViolationError` 的公共父类，语义为“受控退出，非普通失败”。
- `ReActAgent.run()` 已统一 `except AgentControlError as e:` 处理这一族异常：持久化已收集内容、构造 `AgentResult`、`emit_complete`。
- `InterceptorChain` 对 `AgentControlError` 一律透传，不做错误转换。
- `HookRunner.dispatch()` 会吞掉普通 hook 异常（LOG 策略仅记录），但 `asyncio.CancelledError` 透传。

把循环检测纳入这套受控退出模型，比“hook 设标志 + LLMNode 再分支检查”更内聚：检测、决策、退出收敛在同一个异常语义里，不污染 ReAct 节点的流程判断。

## Decision

### 1. 把循环检测做成一种受控退出异常

新增异常 `LoopDetectedError(AgentControlError)`，与 `AgentCancelledError` 同族。让父类 `AgentControlError` 携带**两个可覆盖属性**，统一描述退出时给用户的反馈：

```python
class AgentControlError(Exception):
    """受控退出基类。"""

    # 退出时要展示给用户的内容（XML/文本）。子类可覆盖；默认空。
    user_content: str = ""
    # 退出原因，对应 StopReason。子类可覆盖；默认 CANCELLED。
    stop_reason: "StopReason" = StopReason.CANCELLED

    def __init__(self, reason: str = "") -> None:
        super().__init__(reason)
```

`AgentCancelledError` / `AgentTimeoutError` / `PolicyViolationError` 保持原 `__init__` 签名不变，仅继承默认属性（`user_content=""`、`stop_reason` 各自定义：`CANCELLED` / `TIMEOUT` / `ERROR`，**只补不破**）。

新增：

```python
class LoopDetectedError(AgentControlError):
    """检测到 ReAct 循环，强制结束当前 turn。"""

    user_content: str
    stop_reason: StopReason = StopReason.LOOP_DETECTED  # 新增枚举值

    def __init__(self, user_content: str, loop_type: str) -> None:
        super().__init__(f"Loop detected ({loop_type})")
        self.user_content = user_content
        self.loop_type = loop_type
```

> 说明：`StopReason` 是 `StrEnum`，`stop_reason: "StopReason"` 作为类标注是 typing 字符串前向引用；实际类型来自 `core.constants`。

### 2. `ReActAgent.run()` 退出处理统一读异常属性

现有 `except AgentControlError as e:` 块改为使用异常的 `user_content` / `stop_reason`：

```python
except AgentControlError as e:
    logger.info("ReActAgent control exit: %s", str(e) or "error")
    await _persist_interrupted_partial(context, _interrupt_reason_from(e))
    all_new = _get_turn_messages(context)
    result = AgentResult(
        content=getattr(e, "user_content", "") or "",
        stop_reason=getattr(e, "stop_reason", StopReason.CANCELLED),
        messages=all_new,
        attachments=context.attachments,
    )
    await emitter.emit_complete(result)
    return result
```

`getattr` 带默认值是为防御：第三方异常若继承 `AgentControlError` 但未设这两个属性也不会崩。

### 3. `HookRunner` 透传 `AgentControlError`

`src/modex_agent/hook/runner.py` 的 `dispatch()` 异常分支增加透传：

```python
except asyncio.CancelledError:
    raise
except AgentControlError:
    raise  # 控制异常不是 hook 失败，透传到 ReActAgent.run()
except TimeoutError:
    ...
except Exception:
    ...
```

语义正当：`AgentControlError` 是**有意为之的退出信号**，不是 hook 执行错误，不该被 LOG/IGNORE 策略吞掉。

### 4. 检测 Hook：`LoopDetectionHook`（两阶段守护）

`src/modex_agent/hook/builtin/loop_detection.py`：

```python
class LoopDetectionHook(BeforeIterationHook):
    """每次 LLM 调用前，从历史推导尾部重复轮；两阶段：窗口提醒 → 观察期 → 受控退出。"""
```

#### 槽位：`before_iteration`（而非 `after_llm_response`）

检测移动到 LLM 调用**前**的 hook 槽位，理由：

1. **提醒当场生效**：`LLMNode.actual_iteration` 的顺序是 `BEFORE_ITERATION hook → _build_messages → BEFORE_LLM → LLM 调用`。在 `BEFORE_ITERATION` 注入的 system-reminder 会进入**本次**请求（`normalize_agent_messages_for_llm` 将 `system_reminder` role 替换为 `user`）；若放在 `BEFORE_LLM`（messages 已构建）或 `after_llm_response`（本轮已烧完一次响应），提醒要迟到一轮。
2. **硬断不再浪费一次调用**：退出发生在下一轮 LLM 调用之前。
3. **与既有注入机制同槽**：`InjectionDrainer`、`InboxFlushHook` 均在 `before_iteration` 注入历史 —— 这是「影响即将发生的 LLM 调用」的既有收敛点。

#### 信号：尾部重复轮（trailing repeat run）

**一轮（round）** = 一条带 tool_calls 的 assistant 消息。轮身份（identity）= 工具调用批次的 `(frozenset[(tool_name, canonical_args)], 调用数)` —— 名称与参数（JSON 按 key 排序归一化，忽略 call_id 与顺序）完全相同**且**每轮调用数相同（`[read/a, read/a]` 与 `[read/a]` 因此可区分）。批内多工具（`[read/a, ls/b]`）作为整体身份参与比较，批次循环同样可检测。

`_trailing_repeat_run` 反向扫描 history，统计**尾部连续同身份的轮数 L**：

- **唯一的边界是纯 `user` 消息**（`MessageRole.USER` 严格相等）——用户插话改变上下文，跨 user 的重复不是循环。
- **其余全部透明（跳过、不打断）**：`tool` 结果、`system_reminder`（含本 hook 自己注入的提醒 —— 否则提醒会重置自己的计数）、`agent` 消息、`compact` 标记、无工具的 assistant 文本（「调用→评论→调用」式循环照样累计）。
- **扫描预算按轮计**（`scan_cap = 2×window_size + 3`，派生不可配）：计数满 `scan_cap` 轮即停止向前扫描。预算只被轮消耗 —— 透明消息不耗预算。防护「无 user 边界」的 history（compaction 可能清掉最后一条 user 消息；跨 run 累积的病态 session）。计数钉在 `scan_cap` 时真实轮数 ≥ 该值（下界）：文案以 "at least N" 措辞，注入锚 clamp 到 `scan_cap - observation_rounds` 保持文本自洽。
- 尾部出现不同身份的轮即停止。

**跨 run 语义（免费获得）**：subagent 任务派发 / 父回复 / peer 消息 / 完成通知全部经 `build_agent_reminder_record` 落为 `system_reminder` —— 对扫描透明。因此 L 跨 ReAct run（turn）持续累计，而信号本身从**持久化 history 无状态推导**：父 agent 反复 `task()` 同参数派发（大周期循环）、subagent 跨 invocation 循环、进程重启后循环，均可检测；只有人类输入（role=user）或 agent 真正改变行为（换工具/换参数/换批次形状）才会重置。episode 状态是 per-turn 的（见下），丢失只导致一次冗余提醒，永不导致跳过提醒直接退出。

#### 状态机（episode）

per-turn 状态存 `state.custom[TurnCustomKey.LOOP_EPISODE]`（JSON-safe dict `{"fp": str, "rounds": int, "checks": int}`；`rounds` = 注入锚（clamp 后），`checks` = 注入以来同 fp 的检查次数；`_` 前缀 = 瞬态，审批挂起/恢复丢弃它只损失一次重新提醒）。hook 实例保持无状态（hook/AGENTS.md Rule 1）：

```
每次 before_iteration：
  (identity, L) = _trailing_repeat_run(history, scan_cap)
  无尾部轮 / L < window               → 清除 episode（user steer 打断、循环被打破 —— 无条件原谅）
  episode 存在且 episode.fp != identity → 清除 episode（循环切换）

  L ≥ window 且无 episode            → 阶段一（软）：append system-reminder
                                        （点名重复调用、轮数、要求换方法/换参数/收尾），
                                        记录 episode = {fp, rounds: min(L, scan_cap - observation), checks: 0}

  L ≥ window 且 episode.fp == fp → checks += 1：
      checks < observation_rounds(默认 2)
                                      → 静默观察（不重复提醒 —— 对同一身份的重复提醒无增量信息）
      checks ≥ observation_rounds
                                      → 阶段二（硬）：抛 LoopDetectedError（纯文本 user_content）
```

要点：

- **退出数的是「注入后带提醒可见却未悔改的 LLM 决策次数」（checks），不是 L 的绝对增长**。agent 持续重复时每次检查前恰好新增一轮，checks 与 L 严格同步 —— 常规场景时间线与「观察期再容忍 N 轮」完全一致。关键在饱和场景：L 钉在 `scan_cap` 后不再增长，若退出条件是 `L ≥ 锚 + 观察期` 则永远差 2 而哑火（一次性提醒后永久静默的 livelock）；checks 计数不依赖 L 增长，结构性免疫。
- **观察锚从注入时的真实 L 起算**（跨 run 进入时 L 可能已远超窗口，如 L=15 注入则文本报 "after round 15"），agent 在退出前**必然**在本 run 内见过提醒；L 饱和时锚 clamp 到 `scan_cap - observation`，退出文案的「继续 N 轮」与 checks 一致。
- **run 跌回窗口以下（user steer、循环被打破）即清除 episode**：同一循环复发重新走完整「提醒→观察」，不存在「steer 之后无新提醒却直接退出」的路径。
- **先 append 后写状态**：append 失败被 LOG 策略记录、状态未写，下一次迭代自然重试 —— 不会出现「记了账但模型没见过提醒」。

#### 两段文案

**软提醒**（模型可见，`wrap_system_reminder` 包裹后以 `role=system_reminder` 入史，与 LengthGuardHook / TodoContinuationHook 的注入格式一致）：

```
Repeated tool call detected:
- tool call(s): read({"path": "/a"})
- consecutive rounds: 10

The repeated calls are not making progress — this exact call was already
executed and its result will not change. Do not repeat it again. Inspect
the latest result and choose a different action, different arguments, or
finish the task if enough evidence has been gathered.
```

**硬断 user_content**（纯文本，面向用户 —— 用户不解析 XML；`ReActAgent.run` 的 `except AgentControlError` 块将其写入 `AgentResult.content`）：

```
Loop detected — turn force-ended.

The agent repeated the same tool call(s) for 12 consecutive rounds:
- tool call(s): read({"path": "/a"})

A system reminder was injected after round 10 telling the agent to change
approach, but the repetition continued for 2 more rounds. The turn was
stopped to prevent further wasted calls.

Suggestions: point the agent to different inputs (paths, queries,
parameters), rephrase the request with more specific instructions, or ask
whether this tool can still make progress.
```

#### 与 `ToolCallDeduplicator` 的关系（执行层 streak 守卫）

ToolNode 挂有 per-turn 的 `ToolCallDeduplicator`：单个 `(tool, args)` key 跨相邻工具步骤连续重复时梯度干预（streak 3/5 提醒、8 跳过执行、12 取消 turn，取消走 `phase=CANCELLED` 路由）。两者是**互补而非重复**：

| 维度 | LoopDetectionHook | ToolCallDeduplicator |
|---|---|---|
| 检测面 | 批次身份（名称+参数+调用数） | 单 key 相邻出现 |
| 状态来源 | 持久化 history（跨 run 存活） | per-turn 实例（run 结束即失忆） |
| 终点语义 | `LoopDetectedError` → LOOP_DETECTED + 纯文本说明 | streak 12 → CANCELLED（无循环说明） |
| 抓得住 | 跨 run 累积、批内多工具、`[A,A]` vs `[A]` 批次差异 | 相邻单 key 重复（含被 deny 的调用） |

**竞速参数**：默认 `window_size=10 + observation_rounds=2` 使硬断落在第 13 轮的 `before_iteration` —— 先于该轮 LLM 调用与 ToolNode，因此先于 dedup 的第 13 轮 streak-STOP。用户得到的是带说明的 LOOP_DETECTED 退出而非无声 CANCELLED。`observation_rounds=3` 会让 dedup 抢先（第 13 轮 ToolNode），LOOP_DETECTED 在单 run 场景退化为不可达 —— 调参时必须保持 `window_size + observation_rounds + 1 ≤ 13`（dedup STOP 轮）。dedup 的梯度（streak 3 提醒 / 8 跳过）依旧更早介入执行层，两者共存。

#### 安全闸门

- `enabled=False` 直接返回。
- 非 ReAct 上下文（`get_react_state(ctx) is None`，如 clean mode）直接返回。
- LLM error response 不涉及 —— 检测在 LLM 调用之前，不消费 response。

### 5. 配置

新增 per-pool / per-agent 配置项（设计目标接入现有 `ioc/configs/hooks.py` 的 `HooksConfig`，YAML 可覆盖）：

```yaml
hooks:
  loop_detection:
    enabled: true             # 全局启用（默认 true）
    window_size: 10           # 尾部连续同身份轮数达到即注入提醒，默认 10
    observation_rounds: 2     # 提醒后再容忍的检查轮数，默认 2
```

`window_size` 下限 clamp 到 2（无上限 —— 信号从 history 一次线性扫描推导，窗口大不增加复杂度）；`observation_rounds` 下限 clamp 到 0（0 = 提醒后仅一次 LLM 决策机会）；扫描预算 `scan_cap = 2×window_size + 3` 派生自 window，不可配置。

> **v1 实现说明：** 当前版本按 YAGNI 原则，在 `DefaultAgentFactory.create_agent` 中直接以构造函数默认值装配 `LoopDetectionHook()`，未新增 `HooksConfig` YAML 字段。`enabled` / `window_size` / `observation_rounds` 已通过构造函数参数暴露，测试与后续 wiring 可直接覆盖。YAML 可配置性作为未来增强保留。

#### 为何默认 window=10 / observation=2

- 真实工作流中合理的重复并不少见（多步确认、轮询式探查、迭代逼近）；10 轮连续**同工具同参数同批次形状**的重复几乎不可能是正当行为，旧版 N=5 配合内容相似度的合取虽也保守，但内容相似度会把「每轮都说类似的话但正当地重试」误伤，且完全检测不到跨 run 累积。
- **必须与两个周边预算联动**（调参约束）：
  - `window_size + observation_rounds + 1 ≤ 13`：硬断轮必须不晚于 `ToolCallDeduplicator` 的 streak-STOP（第 13 轮），否则 LOOP_DETECTED 退化为不可达（见 §4 竞速参数）。
  - `window_size + observation_rounds + 1 ≤ max_iterations`（默认 15）：否则硬断被业务层 max-iterations 退出遮蔽（良性退化 —— turn 仍会终止，但用户看到的是 MAX_ITERATIONS 提示而非循环说明）。

### 6. 注册

`LoopDetectionHook` 在每个 ReAct pool 上无条件装配（默认 `enabled=true`，可经配置关闭），在 pool builder 里 `HookSpec(hook=LoopDetectionHook(...))`。配置实例在装配时传入 hook 构造函数。

> **v1 实现说明：** 当前版本在 `DefaultAgentFactory.create_agent` 中无条件装配 `LoopDetectionHook()`，覆盖 main agent 与 subagent。配置来源为构造函数默认值；待产品确认需要 per-agent 调参后，再接入 `HooksConfig` / pool-builder 传参。

### 7. main agent vs subagent 的通知路由

`LoopDetectedError` 抛出后，`ReActAgent.run()` 的 `except AgentControlError` 块构造的 `AgentResult(content=<纯文本循环说明>, stop_reason=LOOP_DETECTED)`，其去向取决于 agent 的 `comm_kind`，**无需在循环检测逻辑里分支**——复用现有通知路由即可：

#### main agent（`comm_kind == NORMAL`）

纯文本循环说明作为 `result.content`，经 `emit_complete(result)` 送达**用户**（WebUI/IM 收到可直接阅读的说明与建议）。`TurnOutcomeNotifyHook` 只对 MAX_ITERATIONS/ERROR 发纯文本提示，对 `loop_detected` 不重复打扰——`emit_complete` 已带说明内容。符合"用户收到循环说明"。

> **实现验证点**：main agent 路径依赖 `emit_complete(result)` 把 `result.content`（循环说明）真正回放给用户。流式路径下说明文本是在 catch 块新构造的（不是流式已发送的内容），需确认 `StreamingAwareEmitter.emit_complete` 对 `result.content` 非空时的回放语义。若实测发现流式 emitter 在 stream_end 后不再回放 content，则在 `except AgentControlError` 块里显式补一次 `emitter.emit_content(e.user_content)`。测试用例需覆盖此断言。

#### subagent（`comm_kind == SUBAGENT`）

subagent 的结果**不应直达用户**，而应回到父 agent，由父 agent 决定后续。现有链路已天然支持：

1. subagent `ReActAgent.run()` catch 后构造 `AgentResult(stop_reason=LOOP_DETECTED, content=<纯文本循环说明>)`。
2. `finally` 块触发 `SubagentAutoSendHook.finally_turn`：读取 `result.stop_reason`/`result.content`，构造 `<subagent_notification>` XML，经 `agent_bus` 发到**父 agent inbox**。

**问题（必须修）**：`SubagentAutoSendHook._classify_stop` 的非正常退出集合 `_NON_NORMAL_STOPS = {max_iterations, turn_cancelled, timeout}` 不含 `loop_detected`，会导致循环被误判为"正常完成"或"只是没写 OUTPUT.md"，父 agent 收不到明确信号。

**修复**：

1. 把 `"loop_detected"` 加入 `_NON_NORMAL_STOPS`。
2. 在 `_classify_stop` 增加专属分支，给出"子代理陷入循环"的可读 hint：

```python
if stop_reason == "loop_detected":
    return False, (
        f"Subagent stopped with {stop_reason} — it was stuck in a loop "
        f"(repeating the same output or the same tool calls). Task is incomplete."
        f"{resume}"
    )
```

修复后，父 agent 收到的通知形如：

```xml
<subagent_notification>
  <agent>scout</agent>
  <status>incomplete</status>
  <stop_reason>loop_detected</stop_reason>
  <is_normal>false</is_normal>
  <hint>Subagent stopped with loop_detected — it was stuck in a loop ...
        To continue, send a message with invocation_id=...</hint>
  <summary>Loop detected — turn force-ended. The agent repeated the same
        tool call(s) for 12 consecutive rounds ...</summary>
  <artifacts>...</artifacts>
</subagent_notification>
```

`<summary>` 字段已携带循环说明的截断内容（`_truncate_content`，上限 1500 字符），父 agent 据此可看到重复的工具调用与参数、提醒已注入仍无效的事实，从而决定是换参数重派、自行接管，还是放弃。

#### 设计要点

- 循环检测 hook 本身**完全不知道**自己是 main 还是 subagent——它只负责"检测到就抛 `LoopDetectedError`"。
- 路由由 `comm_kind` + 现有 `SubagentAutoSendHook` / `TurnOutcomeNotifyHook` / `emit_complete` 决定。
- 唯一需要改的是 `_NON_NORMAL_STOPS` 与 `_classify_stop`，让 subagent 路径正确识别 `loop_detected`。

## Consequences

### 正面

- **先软后硬**：绝大多数循环在提醒后即被打破（模型看到点名批评的 system-reminder 后换方法）；硬断是最后手段，且退出时用户收到纯文本说明（哪里循环、提醒过几次、为何终止）。
- **跨 run 检测免费获得**：信号从持久化 history 无状态推导，agent 间消息全部是透明 role —— 大周期循环（父 agent 反复同参派发）、subagent 跨 invocation 循环、进程重启后的循环均可检测，无需任何持久化新设施。
- **改动集中**：仅重写 1 个 hook 文件 + 1 个 `TurnCustomKey`；异常模型、runner 透传、run() 退出块、装配点、subagent 通知路由全部复用不动。
- **可测**：检测是无状态纯函数（`_trailing_repeat_run`）+ 显式 episode 状态机，可通过 `before_iteration(ctx)` 公共接口做单元测试。

### 负面 / 已知局限

- **周期 > 1 的轮转循环（A,B,A,B）仍漏判**：尾部连续同身份判定只覆盖完全重复。有意保留（用户确认的简化范围）；周期检测是后续增强。
- **短 run 逃逸**：若 agent 每 run 只循环 1-2 轮即结束 turn、再被外部反复拉起，每 run 入口都会触发提醒，但单 run 内永远凑不满「注入时 L + 观察期」的退出条件 —— 无限提醒、永不硬断。周边守卫（TodoContinuation 签名反死锁、MAX_TURNS、LengthGuard 10 次上限）各自有界。彻底闭合需要跨 run 的 episode 持久化 —— **已知且有意推迟**（涉及持久化设计）；未来若要闭合，最自然的路径是把 fp 嵌入提醒文本、从 history 推导「已提醒」状态（history 即现成的持久层）。
- **参数耦合**：`window_size + observation_rounds + 1` 必须同时 ≤ 13（dedup STOP 轮）与 ≤ `max_iterations`（默认 15），否则硬断被遮蔽为良性退化（见 §5）。
- **每轮一次历史线性扫描（上限 `scan_cap` 轮）**：`before_iteration` 全量 `to_list()`（ScopedMessageHistory 缓存命中，无落盘 IO）+ 有界反向扫描。关闭开关保留。
- **合法的长程同参轮询**（如 watch 式探查）会在 10+2 轮被终止 —— 与所有循环检测共享的固有取舍；压力阀是 `window_size` / `enabled` 配置。

## 实现变更清单（实现时核对）

1. `src/modex_agent/core/constants.py` — `StopReason` 新增 `LOOP_DETECTED = "loop_detected"`。
2. `src/modex_agent/control/exceptions.py` — `AgentControlError` 加 `user_content`/`stop_reason` 类属性默认值；`AgentCancelledError`/`AgentTimeoutError`/`PolicyViolationError` 各自 `stop_reason` 默认值；新增 `LoopDetectedError`。
3. `src/modex_agent/hook/runner.py` — `dispatch()` 新增 `except AgentControlError: raise` 透传分支。
4. `src/modex_agent/agents/react/agent.py` — `except AgentControlError` 块改用 `e.user_content` / `e.stop_reason` 构造 `AgentResult`。
5. `src/modex_agent/hook/builtin/loop_detection.py` — `LoopDetectionHook` 重写为 `BeforeIterationHook`：`_round_identity` / `_trailing_repeat_run` / `_identity_preview` + episode 状态机 + 两段纯文本文案；删除相似度机制（`_similarity` / `_collect_recent_assistants` / `_build_loop_xml` / 合取判定）。`_canonical_args` / `_extract_tool_pairs` / `_tool_calls_fingerprint` / `_tool_calls_count` 保留复用。
6. `src/modex_agent/hook/builtin/__init__.py` — 导出 `LoopDetectionHook`（不变）。
7. `src/modex_agent/ioc/configs/hooks.py` — （未来增强）`HooksConfig` 增 `loop_detection` 子配置（`enabled` / `window_size` / `observation_rounds`）。当前使用构造函数默认值在 `DefaultAgentFactory` 中装配。
8. pool builder（`examples/bot_project/bot/service/pool_builder.py` 或对应装配点）— v1 通过 `DefaultAgentFactory` 无条件装配 `LoopDetectionHook()`；未来可在此传入配置。
9. `src/modex_agent/hook/builtin/subagent_auto_send.py` — `_NON_NORMAL_STOPS` 加入 `"loop_detected"`；`_classify_stop` 增加 `loop_detected` 专属 hint 分支，确保 subagent 循环正确路由到父 agent 而非被误判为正常完成。（首版已落地，本修订不涉及。）
10. `src/modex_agent/runtime/enums.py` — `TurnCustomKey` 新增 `LOOP_EPISODE = "_loop_episode"`（2026-08-28 修订）。

### 测试

- 单元：`_tool_calls_fingerprint` 忽略 call_id、参数顺序无关；`_tool_calls_count` 保留重复（与去重 fingerprint 区分）；`_trailing_repeat_run` 的边界（user 打断、tool/system_reminder/agent/compact/无工具 assistant 透明、身份切换断 run、批次 vs 单调用、空历史）。
- hook 状态机：窗口内无动作；窗口命中注入提醒 + 记录 episode；观察期静默（不重复提醒不退出）；观察期满抛 `LoopDetectedError`（纯文本 user_content 含轮数、注入轮、继续轮数、无 XML）；换身份即原谅（episode 清除 + 重新注入）；超过窗口进入（跨 run 场景，注入轮 = 真实 L）；fresh turn 对同一循环重新注入；`observation_rounds=0` / `enabled=False` / 非 ReAct 上下文。
- runner 透传：mock hook 抛 `AgentControlError`，`dispatch()` 透传而非吞掉（既有测试，不变）。
- main agent 退出：`LoopDetectedError` 经 `ReActAgent.run()` 产出 `AgentResult(stop_reason=LOOP_DETECTED, content=<纯文本说明>)`，`emit_complete` 送达用户。
- subagent 路由：subagent `comm_kind` 下 `LoopDetectedError` → `SubagentAutoSendHook` 产出 `status=incomplete`、`stop_reason=loop_detected`、hint 含"stuck in a loop" 的通知，发往**父 inbox**（断言不触发用户 `_notify_user` 路径）。
