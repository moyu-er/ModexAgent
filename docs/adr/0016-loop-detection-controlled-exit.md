# ADR-0016: ReAct 循环检测作为受控退出

- **Status:** Accepted
- **Date:** 2026-07-07
- **Related:** ADR-0008（审批主 agent 镜像与 sanitizer）、`TodoCompletionProbeHook`（`src/modex_agent/tools/standard/todo_probe.py`）、`AgentControlError` 退出模型（`src/modex_agent/control/exceptions.py`）

## Context

### 问题

某些模型在 ReAct 循环中会陷入两类死循环，空耗 token、永不结束 turn：

1. **内容循环**：连续多次 assistant 输出高度相似甚至相同的纯文本回复（无工具调用）。
2. **工具循环**：连续多次调用**同名 + 同参数**的工具（例如反复 `read` 同一个 path、反复 `ls` 同一目录）。

现有机制无法检测：`max_iterations` 只数迭代次数，不关心内容是否重复；`TodoCompletionProbeHook` 只在“有未完成 todo 且想结束 turn”时介入，与死循环无关。

### 现有架构契合点

项目已有一套统一的“受控退出”模型：

- `AgentControlError` 是 `AgentCancelled` / `AgentTimeout` / `PolicyViolation` 的公共父类，语义为“受控退出，非普通失败”。
- `ReActAgent.run()` 已统一 `except AgentControlError as e:` 处理这一族异常：持久化已收集内容、构造 `AgentResult`、`emit_complete`。
- `InterceptorChain` 对 `AgentControlError` 一律透传，不做错误转换。
- `HookRunner.dispatch()` 会吞掉普通 hook 异常（LOG 策略仅记录），但 `asyncio.CancelledError` 透传。

把循环检测纳入这套受控退出模型，比“hook 设标志 + LLMNode 再分支检查”更内聚：检测、决策、退出收敛在同一个异常语义里，不污染 ReAct 节点的流程判断。

## Decision

### 1. 把循环检测做成一种受控退出异常

新增异常 `LoopDetectedError(AgentControlError)`，与 `AgentCancelled` 同族。让父类 `AgentControlError` 携带**两个可覆盖属性**，统一描述退出时给用户的反馈：

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

`AgentCancelled` / `AgentTimeout` / `PolicyViolation` 保持原 `__init__` 签名不变，仅继承默认属性（`user_content=""`、`stop_reason` 各自定义：`CANCELLED` / `TIMEOUT` / `ERROR`，**只补不破**）。

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

### 4. 检测 Hook：`LoopDetectionHook`

新增 `src/modex_agent/hook/builtin/loop_detection.py`：

```python
class LoopDetectionHook(AfterLLMResponseHook):
    """每次 assistant 完整 response 拿到后，无状态检测内容/工具循环。
    命中即抛 LoopDetectedError，结束当前 turn。
    """
```

**无状态**：检测逻辑不在 `ReActTurnState.custom` 存任何计数/fingerprint，每次 `after_llm_response` 独立从历史读取、判断。

#### 输入读取

通过 `await ctx.history.to_list()` 拿到 `list[ChatMessage]`。从末尾向前扫描，构造“当前 turn 的连续 assistant 序列”：

- 跳过（忽略）`role == tool` 的消息（工具结果是 ReAct 中间态，不属于 assistant 输出）。
- 遇到 `role == user` 立即停止（user 消息标志新 turn 开始，再往前属另一 turn）。
- 只收集 `role == assistant` 的消息，直到收集到 N 条或窗口被打断。

> 注意：当前刚返回的 response 对应的 assistant 消息此时**尚未** append 到 history（`after_llm_response` 在 `ctx.history.append(assistant_msg)` **之前**调度，见 `nodes/llm.py:149` vs `:183`）。因此待检测序列 = 「history 末尾的连续 assistant 消息」+「本次 response」。Hook 把本次 response 虚拟成一条 assistant 消息补到序列尾部参与比较。

#### 判定规则

设可配置窗口 `N`（默认 **5**，见 §5）。当连续 assistant 消息不足 N 条时，不判定。

**A. 内容循环（content loop）**：

仅取**归一化后非空**的 assistant `content` 参与内容循环判定（含 tool_calls 但 content 为空的 assistant 消息**跳过**，避免工具循环被误报成内容循环）。若当前 turn 末尾连续的、非空 content 的 assistant 消息达到 N 条，且这 N 条文本**两两相似度都 ≥ 阈值** `content_similarity_threshold`（默认 **0.85**），判定为内容循环。

相似度函数：纯 Python 实现，无外部依赖。采用归一化后的 **SequenceMatcher.ratio()**（`difflib`，标准库）。输入先归一化，再截断到固定样本长度（与 XML 输出截断一致，默认 500 字符），避免长上下文下 `SequenceMatcher` 的 O(n²) 开销：

```python
def _normalize_text(s: str) -> str:
    # 折叠空白、统一大小写、去首尾
    return " ".join(s.lower().split()).strip()

def _similarity(a: str, b: str) -> float:
    na = _normalize_text(a)[:500]
    nb = _normalize_text(b)[:500]
    if not na and not nb:
        return 1.0
    if not na or not nb:
        return 0.0
    return SequenceMatcher(None, na, nb).ratio()
```

阈值在调用方 `content_similarity_threshold` 控制（默认 **0.85**），`_similarity` 只返回 ratio。

**B. 工具循环（tool loop）**：

仅取**含非空 tool_calls**的 assistant 消息参与工具循环判定。若当前 turn 末尾连续的、含工具调用的 assistant 消息达到 N 条，则对每条消息计算其工具调用集合的 fingerprint：

```python
def _tool_fingerprint(tool_calls: list[ToolCall]) -> frozenset[tuple[str, str]]:
    # (tool_name, arguments_json_canonical)。忽略 call_id。
    # arguments 用 json.dumps(sort_keys=True, ensure_ascii=False) 归一化。
    return frozenset(
        (tc.tool_name, json.dumps(tc.arguments or {}, sort_keys=True, ensure_ascii=False))
        for tc in tool_calls
    )
```

若连续 N 条 assistant 消息的 tool fingerprint **完全相同且非空**（即同名同参数的工具集合逐条重复），判定为工具循环。

> 单条 assistant 内重复同名同参工具不算（那是单次决策）；必须**跨多条 assistant 消息**重复才构成循环。

**触发**：A 或 B 任一命中，Hook 构造英文 XML 说明（见下）并抛 `LoopDetectedError(user_content=xml, loop_type="content"|"tool")`。

#### XML 反馈内容（英文，写死）

`loop_type == "content"`：

```
<loop_detected type="content">
The agent produced the same text output N times in a row and appears stuck in a loop.
Last output (truncated to 500 chars):
<last_output>{escaped_content}</last_output>

How to break out:
- Rephrase your request with more specific instructions or constraints.
- Ask the agent to use a different approach or tool.
- If the goal is already met, tell the agent to stop.
</loop_detected>
```

`loop_type == "tool"`：

```
<loop_detected type="tool">
The agent repeatedly called the same tool(s) with identical arguments N times and appears stuck in a loop.
Repeated tool(s): {tool_names}
Last repeated arguments (truncated to 500 chars per call):
<repeated_calls>{escaped_args}</repeated_calls>

How to break out:
- Point the agent to different inputs (paths, queries, parameters).
- Ask the agent to reconsider whether this tool can make progress.
- If the task is done, tell the agent to stop.
</loop_detected>
```

> 截断长度 500 字符为写死常量。转义使用 `modex_agent.utils.xml.xml_text`。

#### 安全闸门

- LLM error response（`finish_reason == "error"`）直接 return，不检测（turn 本身就要结束）。
- 本次 response 为空（无 content 且无 tool_calls）直接 return。
- 不足 N 条直接 return。

### 5. 配置

新增 per-pool / per-agent 配置项（设计目标接入现有 `ioc/configs/hooks.py` 的 `HooksConfig`，YAML 可覆盖）：

```yaml
hooks:
  loop_detection:
    enabled: true            # 全局启用（默认 true）
    window_size: 5           # 连续 N 条 assistant，默认 5
    content_similarity_threshold: 0.85  # 默认 0.85
```

`window_size` 取值范围 `[2, 8]`，越界 clamp 到范围内并 warn。

> **v1 实现说明：** 当前版本按 YAGNI 原则，在 `DefaultAgentFactory.create_agent` 中直接以构造函数默认值装配 `LoopDetectionHook()`，未新增 `HooksConfig` YAML 字段。`enabled` / `window_size` / `content_similarity_threshold` 已通过构造函数参数暴露，测试与后续 wiring 可直接覆盖。YAML 可配置性作为未来增强保留。

#### 为何默认 N=5

- 真实工作流中合理的重复并不少见（多步确认、轮询式探查、迭代逼近），窗口太小（2-3）会把正常重复误判成循环、打断正当任务。
- N=5 要求连续 5 次相同才介入，大幅降低误报；代价是循环多烧几轮，但相对"误中断正当任务"是可接受的小损失。
- 用户可按 agent 调小（激进，如 2-3）或更大（极保守）。

### 6. 注册

`LoopDetectionHook` 在每个 ReAct pool 上无条件装配（默认 `enabled=true`，可经配置关闭），与 `TodoCompletionProbeHook` 的装配方式一致（在 pool builder 里 `HookSpec(hook=LoopDetectionHook(...))`）。配置实例在装配时传入 hook 构造函数。

> **v1 实现说明：** 当前版本在 `DefaultAgentFactory.create_agent` 中无条件装配 `LoopDetectionHook()`，覆盖 main agent 与 subagent。配置来源为构造函数默认值；待产品确认需要 per-agent 调参后，再接入 `HooksConfig` / pool-builder 传参。

### 7. main agent vs subagent 的通知路由

`LoopDetectedError` 抛出后，`ReActAgent.run()` 的 `except AgentControlError` 块构造的 `AgentResult(content=<loop_xml>, stop_reason=LOOP_DETECTED)`，其去向取决于 agent 的 `comm_kind`，**无需在循环检测逻辑里分支**——复用现有通知路由即可：

#### main agent（`comm_kind == NORMAL`）

循环 XML 作为 `result.content`，经 `emit_complete(result)` 送达**用户**（WebUI/IM 收到英文 XML 说明）。`TurnOutcomeNotifyHook` 只对 MAX_ITERATIONS/ERROR 发纯文本提示，对 `loop_detected` 不重复打扰——`emit_complete` 已带 XML 内容。符合"用户收到循环说明"。

> **实现验证点**：main agent 路径依赖 `emit_complete(result)` 把 `result.content`（循环 XML）真正回放给用户。流式路径下循环 XML 是在 catch 块新构造的（不是流式已发送的内容），需确认 `StreamingAwareEmitter.emit_complete` 对 `result.content` 非空时的回放语义。若实测发现流式 emitter 在 stream_end 后不再回放 content，则在 `except AgentControlError` 块里显式补一次 `emitter.emit_content(e.user_content)`。测试用例需覆盖此断言。

#### subagent（`comm_kind == SUBAGENT`）

subagent 的结果**不应直达用户**，而应回到父 agent，由父 agent 决定后续。现有链路已天然支持：

1. subagent `ReActAgent.run()` catch 后构造 `AgentResult(stop_reason=LOOP_DETECTED, content=<loop_xml>)`。
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
  <summary>&lt;loop_detected type="tool"&gt;...&lt;/loop_detected&gt;</summary>
  <artifacts>...</artifacts>
</subagent_notification>
```

`<summary>` 字段已携带循环 XML 的截断内容（`_truncate_content`，上限 1500 字符），父 agent 据此可判断是内容循环还是工具循环、最后在重复什么，从而决定是换参数重派、自行接管，还是放弃。

#### 设计要点

- 循环检测 hook 本身**完全不知道**自己是 main 还是 subagent——它只负责"检测到就抛 `LoopDetectedError`"。
- 路由由 `comm_kind` + 现有 `SubagentAutoSendHook` / `TurnOutcomeNotifyHook` / `emit_complete` 决定。
- 唯一需要改的是 `_NON_NORMAL_STOPS` 与 `_classify_stop`，让 subagent 路径正确识别 `loop_detected`。

## Consequences

### 正面

- **改动集中**：1 个新 hook + 父类 2 个属性 + runner 1 行透传 + run() 退出块改属性读取 + 1 个新 StopReason。不碰 `LLMNode` / `ToolNode` / `EndNode`。
- **架构一致**：循环退出与取消/超时/策略违规同族，复用 `AgentControlError` 的统一退出处理，无需新增 catch 分支。
- **可测**：hook 是纯函数式检测 + 抛异常，可通过 `after_llm_response(ctx, response)` 公共接口做单元测试（构造带连续重复 assistant 的 history）。
- **用户可感知**：循环发生时用户/WebUI/IM 收到明确的英文 XML 说明与跳出建议。

### 负面

- **N=3 仍可能漏判**：交替式循环（A,B,A,B）不会被“连续 N 条相同”捕获。本设计有意只覆盖最常见、最明确的完全重复；交替循环留给后续增强。
- **SequenceMatcher 相似度**对长文本开销随长度平方级增长；已通过归一化 + 输入截断（500 字符样本）避免长上下文下的性能问题。XML 输出也截断到 500 字符。
- **全局默认启用**意味着所有 pool 多一次历史扫描 + 相似度计算的开销。历史通常不大，且仅在每轮 assistant 完成后算一次；可接受。关闭开关保留。
- **chunk 级早期检测不在本 ADR 范围**（用户确认后续再补）。届时可在 `ReactLlmClient` / LLM_STREAM interceptor 增量检测重复片段，复用本 ADR 的相似度函数与 `LoopDetectedError`。

## 实现变更清单（实现时核对）

1. `src/modex_agent/core/constants.py` — `StopReason` 新增 `LOOP_DETECTED = "loop_detected"`。
2. `src/modex_agent/control/exceptions.py` — `AgentControlError` 加 `user_content`/`stop_reason` 类属性默认值；`AgentCancelled`/`AgentTimeout`/`PolicyViolation` 各自 `stop_reason` 默认值；新增 `LoopDetectedError`。
3. `src/modex_agent/hook/runner.py` — `dispatch()` 新增 `except AgentControlError: raise` 透传分支。
4. `src/modex_agent/agents/react/agent.py` — `except AgentControlError` 块改用 `e.user_content` / `e.stop_reason` 构造 `AgentResult`。
5. `src/modex_agent/hook/builtin/loop_detection.py` — 新增 `LoopDetectionHook` + 相似度/归一化/工具 fingerprint 辅助函数 + XML 模板。
6. `src/modex_agent/hook/builtin/__init__.py` — 导出 `LoopDetectionHook`。
7. `src/modex_agent/ioc/configs/hooks.py` — （未来增强）`HooksConfig` 增 `loop_detection` 子配置（`enabled` / `window_size` / `content_similarity_threshold`）。v1 使用构造函数默认值在 `DefaultAgentFactory` 中装配。
8. pool builder（`examples/bot_project/bot/service/pool_builder.py` 或对应装配点）— v1 通过 `DefaultAgentFactory` 无条件装配 `LoopDetectionHook()`；未来可在此传入配置。
9. `src/modex_agent/hook/builtin/subagent_auto_send.py` — `_NON_NORMAL_STOPS` 加入 `"loop_detected"`；`_classify_stop` 增加 `loop_detected` 专属 hint 分支，确保 subagent 循环正确路由到父 agent 而非被误判为正常完成。

### 测试

- 单元：`_similarity` 边界（空串、完全相同、相似、不同）；`_tool_fingerprint` 忽略 call_id、参数顺序无关；`_collect_recent_assistants` 窗口被 user 打断、忽略 tool。
- hook 集成：构造 history 连续 N 条相同内容 → 抛 `LoopDetectedError` 且 `loop_type=="content"`；连续 N 条同参工具 → `loop_type=="tool"`；内容循环忽略空 content 消息、工具循环忽略空 tool_calls 消息；不足 N 条 / 被 user 打断 → 不抛；LLM error → 不抛。
- runner 透传：mock hook 抛 `AgentControlError`，`dispatch()` 透传而非吞掉。
- main agent 退出：`LoopDetectedError` 经 `ReActAgent.run()` 产出 `AgentResult(stop_reason=LOOP_DETECTED, content=<xml>)`，`emit_complete` 送达用户。
- subagent 路由：subagent `comm_kind` 下 `LoopDetectedError` → `SubagentAutoSendHook` 产出 `status=incomplete`、`stop_reason=loop_detected`、hint 含"stuck in a loop" 的通知，发往**父 inbox**（断言不触发用户 `_notify_user` 路径）。
