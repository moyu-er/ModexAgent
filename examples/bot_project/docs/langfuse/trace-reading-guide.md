# Trace 追踪实操指南

本指南教你**在 Langfuse 面板里读懂** agent 的运行轨迹 — 各个 span 是什么、
12 个指标怎么读、trace_id / span_id 的含义、以及怎么用筛选和排序定位问题。

部署和配置见 `langfuse-deployment.md`;eval 和 golden cassette 见同文件 §4-5。
本指南只讲:trace 到了 Langfuse 之后,**你怎么看懂它**。

## 1. 三个核心概念

Langfuse v4 用三个层级组织数据:

| 概念 | 含义 | 在 ModexAgent 里对应什么 |
|------|------|--------------------------|
| **Observation** | 一次操作(LLM 调用、工具执行、审批决策等) | 一个 span(chat / execute_tool / human.review 等) |
| **Trace** | 共享同一个 `trace_id` 的所有 observation 的集合 | 一个 agent turn(从用户消息到 agent 返回) |
| **Session** | 多个 trace 的分组(同一个对话线程) | 一个 conversation_id 下的所有 turn |

关键区别:**trace = 一个 turn,不是整个对话**。用户发一条消息,agent 跑完一个
ReAct 循环返回结果,这就是一个 trace。下一个消息是另一个 trace。它们通过
`session_id`(即 conversation_id)关联在同一个 session 下。

Langfuse v4 的 UI 主表是 **observations 表**(不是 traces 表)—— 每一行是一个
observation(span),不是一整个 trace。默认视图只显示 **Root Observation**
(`Is Root Observation = true`),即每个 trace 的根节点,方便你看入口点。
点击根节点可以展开看它下面的所有子 observation。

## 2. 打开面板看第一个 trace

1. 浏览器打开 `http://localhost:3000`
2. 左侧导航 → **Observations**(v4 主表)或 **Traces**(传统视图)

**v4 Observations 表的推荐操作:**

- 打开时默认过滤 `Is Root Observation = true` — 你看到的是每个 trace 的入口
- 左侧筛选栏显示可用的筛选值和计数
- 点一个 root observation → 右侧或下方展开该 trace 的完整 span 树
- 保存 2-3 个常用视图(Saved Views)方便一键切换

## 3. Span 树结构 — 每个 span 是什么

一个完整的 agent turn 在 Langfuse 里长这样(v4 observation type 标注在括号里):

```
invoke_agent (type=AGENT)              ← 根 observation,整个 turn
├── agent.start (type=SPAN)            ← 系统提示 + 工具定义(full tier)
├── iteration.start (type=SPAN)        ← ReAct 第 1 轮开始
│   ├── chat (type=GENERATION)         ← LLM 调用:模型、token、延迟、缓存
│   ├── execute_tool_batch (type=SPAN) ← 工具批次
│   │   ├── execute_tool (type=TOOL)   ← 单个工具执行
│   │   └── execute_tool (type=TOOL)   ← 另一个工具
│   └── iteration.end (type=SPAN)      ← 第 1 轮结束
├── iteration.start (type=SPAN)        ← ReAct 第 2 轮(如果需要)
│   ├── chat (type=GENERATION)
│   └── iteration.end (type=SPAN)
└── training_tag (type=EVENT)          ← 训练相关性标记(是否适合用于训练数据)
```

### 每个 span 的含义和看什么

| Span 名 | Observation Type | 出现在 | 重点关注 |
|---------|-----------------|--------|----------|
| `invoke_agent` | AGENT | 每个 turn(根) | `stop_reason` 属性:completed / max_iterations / error / cancelled — 这是 turn 的结局 |
| `agent.start` | SPAN | full tier | 系统提示文本、工具定义列表 — 看 agent 被注入了什么上下文 |
| `iteration.start` / `iteration.end` | SPAN | full tier | ReAct 循环轮数 — 多少轮才完成任务?超过 15 轮可能在打转 |
| `chat` | GENERATION | 每次 LLM 调用 | **最重要的 span** — 模型名、token 用量、延迟、缓存命中、tool_calls |
| `execute_tool_batch` | SPAN | 每次工具批次 | 一次 LLM 响应里调了几个工具 |
| `execute_tool` | TOOL | 每个工具执行 | 工具名、成功/失败、错误类型、执行结果、耗时 |
| `human.review` | SPAN | 审批触发时 | 审批决策(approved/denied)、拒绝原因、触发的工具 |
| `agent.handoff` | SPAN | 子 agent 派发时 | 目标 agent 名、消息类型 — 多 agent 协作时看谁被叫了 |
| `training_tag` | EVENT | 每个 turn 结束 | `gen_ai.training.relevant` 布尔值 — 这个 turn 是否适合做训练数据 |

### `chat` span 详解(LLM 调用,最常看)

`chat` span 的 attributes 里携带 `gen_ai.*` 语义约定属性。Langfuse v4
服务端**自动映射**这些 OTel 属性到结构化字段(model / usageDetails /
input / output),不需要显式写 `langfuse.observation.*` 前缀。

**OTel 属性 → Langfuse 结构化字段映射**(已验证):

| OTel 属性(`gen_ai.*`) | Langfuse 字段 | 说明 |
|------------------------|---------------|------|
| `gen_ai.request.model` | `model` | 模型名,可直接在 UI 按模型筛选 |
| `gen_ai.usage.input_tokens` | `usageDetails.input` | 输入 token(含缓存,Langfuse 自动减去 cached 得到互斥值) |
| `gen_ai.usage.output_tokens` | `usageDetails.output` | 输出 token |
| `gen_ai.usage.cache_read_input_tokens` | `usageDetails.input_cached_tokens` | 缓存命中 token(Langfuse 从 input 中扣除) |
| `gen_ai.usage.reasoning.output_tokens` | `usageDetails.reasoning.output_tokens` | 推理 token |
| `gen_ai.input.messages` / `gen_ai.prompt` | `input` | LLM 输入消息 |
| `gen_ai.output.messages` / `gen_ai.completion` | `output` | LLM 输出消息 |
| `langfuse.observation.type` | `type` | observation 类型(generation / span / agent 等) |

**关键:usage 互斥 bucket 约定。** `gen_ai.usage.*` 被 Langfuse 当作
**inclusive**(包含缓存 token)。服务端自动做 inclusive → exclusive 转换:
`input_cached_tokens` 从 `input` 中扣除,确保每个 token 只计入一个 bucket。
框架代码只需原样上报 provider 返回的 usage 值,不需要自己做减法。

**查 model/usage 的正确方式。** v4 的 observations 列表 API 默认只返回
`core` + `basic` 字段组。要看 model 和 usage,必须用 `fields` 参数请求
`model` + `usage` 字段组(UI 自动包含;CLI/API 需显式指定):

```bash
# CLI — 查 chat span 的 model + usage
npx langfuse-cli api observations list \
  --fields "core,basic,model,usage,io" \
  --name "chat" --limit 10
```

### chat span 的关键属性速查

| 属性 | 含义 | 怎么用 |
|------|------|--------|
| `gen_ai.request.model` | 请求的模型名 | 筛选特定模型的调用 |
| `gen_ai.usage.input_tokens` | 输入 token 数(含缓存) | 高=上下文太长,检查内存注入是否过度 |
| `gen_ai.usage.output_tokens` | 输出 token 数 | 高=模型话痨,检查 system prompt 是否要求简洁 |
| `gen_ai.usage.cache_read_input_tokens` | 缓存命中的 token | 与 input_tokens 比值 = 缓存命中率 |
| `gen_ai.usage.reasoning.output_tokens` | 推理 token(o1/R1 类模型) | 非推理模型(GPT-4o/Claude/DeepSeek-V3)= 0 |
| `gen_ai.output.tool_calls` | LLM 决定调用的工具 | 看 agent 这一步打算干什么 |
| `gen_ai.output.messages` | LLM 的完整响应 | 含 reasoning_content(如果有) |
| `gen_ai.response.id` | Provider 返回的 completion ID(如 `chatcmpl-xxx`) | 关联 provider 日志;在 Langfuse `metadata.attributes` 里查看 |

**注意:** eval CLI(`python -m bot.eval.cli run`)的 chat span 的 `model`
字段为空 — eval harness 传 `model=None` 给 trace hooks,导致
`gen_ai.request.model` 不被设置。usage/input/output 仍然正确。bot 运行时的
chat span 则 model 字段完整(已验证 `step-3.7-flash` 正确映射)。

**trace 分段字段(environment / version / tags):** 当 `.env` 设置了
`LANGFUSE_ENVIRONMENT` / `LANGFUSE_VERSION` / `LANGFUSE_TAGS` 时,每个 span
都会携带这些属性,Langfuse 自动映射到 trace 级结构化字段:

| 属性 | Langfuse 字段 | 用途 |
|------|---------------|------|
| `langfuse.environment` | `environment` | 按 dev/staging/production 筛选 trace |
| `langfuse.version` | `version` | 按 app/prompt 版本分组,支持 A/B 实验 |
| `langfuse.trace.tags` | `tags` | 自定义标签分类(如 `["eval", "math-qa"]`) |

### `execute_tool` span 详解

| 属性 | 含义 |
|------|------|
| `gen_ai.tool.name` | 工具名(write_file / bash / read 等) |
| `gen_ai.tool.success` | 成功(true)或失败(false) |
| `gen_ai.tool.error_type` | 失败时的错误类型 |
| 工具 result | 执行结果(可能被 governance 截断) |

## 4. trace_id 和 span_id — 它们是什么

### trace_id

- 32 字符十六进制字符串(如 `0f111306b7604259bb2ba1f6452a39e4`)
- **一个 agent turn 一个 trace_id** — 在 `RootSpanHook.start_node_turn` 时生成
- 同一个 turn 内的所有 span 共享这个 trace_id
- 子 agent(通过 `task` 工具派发的)复用父 turn 的 trace_id — 所以多 agent 协作的 span 树是连在一起的
- 在 Langfuse UI 的 URL 里:`/traces/{trace_id}`

### span_id

- 16 字符十六进制字符串
- **一个 span 一个 span_id** — 每个 hook 调用 `_save_span` 时生成
- `parent_span_id` 指向父 span,形成树结构
- 根 span(`invoke_agent`)的 `parent_span_id = null`
- 子 agent 的根 span 的 `parent_span_id` 指向父 agent 的 `agent.handoff` span

### 怎么在 UI 里用它们

- **找某个 turn 的完整轨迹**:在 observations 表筛选 `trace_id = <你的 trace_id>`
- **从 trace 详情页看树**:点 root observation → 展开子节点,树结构按 parent_span_id 自动渲染
- **多 agent 场景**:一个 trace_id 下会有多个 `invoke_agent` span(父 + 子),它们通过 `agent.handoff` span 连接

## 5. 12 个 Trajectory Metrics — 怎么读

每个 **COMPLETED** 的 turn 结束后,框架自动注入 12 个 NUMERIC score 到 Langfuse。
在 trace 详情页的 **Scores** 区域,或在 observations 表按 score 筛选/排序。

### 按用途分组

#### 工具质量 — agent 用工具用得好不好

| 指标 | 值域 | 方向 | 怎么读 |
|------|------|------|--------|
| `tool_success_rate` | 0-1 | 高=好 | 成功工具 / 总工具数。低于 0.8 说明工具经常失败 — 查 `execute_tool` span 的 error_type |
| `tool_call_count` | 整数 | 中性 | 这个 turn 调了几次工具。过高(>20)可能在打转 |
| `error_tool_count` | 整数 | 低=好 | 失败的工具次数。>0 就要查具体哪个工具失败了 |

#### 循环效率 — agent 多快完成任务

| 指标 | 值域 | 方向 | 怎么读 |
|------|------|------|--------|
| `iteration_count` | 整数 | 低=好 | ReAct 循了几轮。3-8 轮健康;>15 轮可能在打转(loop detection 没拦住) |
| `llm_call_count` | 整数 | 中性 | LLM 调了几次。通常 = iteration_count,不等说明有重试 |

#### 成本 — 这个 turn 花了多少 token

| 指标 | 值域 | 方向 | 怎么读 |
|------|------|------|--------|
| `total_input_tokens` | 整数 | 高=贵 | 所有 chat span 的 input_tokens 之和。高=上下文太长 |
| `total_output_tokens` | 整数 | 高=贵 | 所有 chat span 的 output_tokens 之和。高=模型输出太长 |
| `total_reasoning_tokens` | 整数 | 中性 | 推理 token 之和。非推理模型=0;推理模型(o1/R1)这个会很大 |

#### 性能 — 速度和缓存

| 指标 | 值域 | 方向 | 怎么读 |
|------|------|------|--------|
| `api_latency_avg_s` | 浮点秒 | 低=好 | LLM 调用平均壁钟时长。>10s 可能是慢模型或网络问题 |
| `cache_hit_rate` | 0-1 | 高=好 | 缓存命中 token / 总输入 token。>0.8 说明 prompt 缓存工作正常;低=系统提示在变 |

#### 辅助 — 参考信息

| 指标 | 值域 | 方向 | 怎么读 |
|------|------|------|--------|
| `response_token_ratio` | 0-1 | 中性 | output / (input + output)。低=输入远大于输出(上下文重) |
| `has_reasoning` | 0/1 | 中性 | 是否用了推理模型。1=推理模型(o1/R1 类),0=普通模型 |

### 为什么 token 只从 chat span 聚合

`invoke_agent` 根 span 也有 usage 属性,但那是**累积值**(整个 turn 的总和)。
如果从根 span 取,再从 chat span 加一遍,会重复计数。所以 12 指标里的 token
类指标**只从 chat span 聚合**,保证不重复。

### 哪些 turn 没有 12 指标

- `stop_reason != completed` 的 turn(error / cancelled / max_iterations)— 不注入能力分
- legacy 格式的 eval item(简单 `{"query": "..."}`)— 走 `_legacy_task`,没有 runtime services

## 6. 常用筛选操作

### 在 Observations 表(v4 主表)

| 我想看 | 怎么筛 |
|--------|--------|
| 所有 agent turn 入口 | 默认 `Is Root Observation = true` |
| 所有 LLM 调用 | `type = GENERATION` 或 `name = chat` |
| 最贵的 LLM 调用 | `type = GENERATION`,按 `total_cost` 或 input_tokens 降序排 |
| 失败的工具调用 | `name = execute_tool`,`level = ERROR` 或按 `gen_ai.tool.success = false` |
| 某个对话的所有 turn | `session_id = <conversation_id>` |
| 某个 turn 的完整轨迹 | `trace_id = <你的 trace_id>` |
| 多 agent 派发 | `name = agent.handoff` |
| 审批决策 | `name = human.review` |

### 在 Scores 视图

| 我想找 | 怎么筛 |
|--------|--------|
| 最差的轨迹(工具失败多) | `name = tool_success_rate`,按 value 升序排 |
| 最慢的 LLM 调用 | `name = api_latency_avg_s`,按 value 降序排 |
| 缓存效果最好的 | `name = cache_hit_rate`,按 value 降序排 |
| 话痨的 turn | `name = total_output_tokens`,按 value 降序排 |
| 循环打转的 turn | `name = iteration_count`,按 value 降序排(>15 的重点关注) |

### 保存常用视图

v4 支持 **Saved Views** — 配好筛选后点保存,下次一键切换。建议保存:

1. **Root Turns** — `Is Root Observation = true`(默认,看入口)
2. **Failed Tools** — `name = execute_tool` + error 筛选
3. **Slow LLM** — `type = GENERATION` + 按延迟降序

## 7. 读一个 Trace 的完整流程

以一次"agent 帮用户写文件被审批拦下"的 turn 为例:

### Step 1:找到 trace

在 Observations 表筛 `Is Root Observation = true`,按时间倒序,找到最近的
`invoke_agent`。看它的 `stop_reason` 属性:

- `completed` → 正常完成
- `max_iterations` → 循环用完了没解决
- `error` → 出错了(看 error message)
- `cancelled` → 用户取消(`/stop` 或 WebUI 暂停)

### Step 2:看整体结构

点开 root observation,看 span 树有几层:

- 几个 `iteration.start`?→ agent 循了几轮
- 有没有 `execute_tool`?→ 调了什么工具
- 有没有 `human.review`?→ 触发了审批
- 有没有 `agent.handoff`?→ 派发了子 agent

### Step 3:钻入 chat span

找到第一个 `chat` span,看 attributes:

- `gen_ai.request.model` → 用的什么模型
- `gen_ai.output.tool_calls` → LLM 决定调什么工具
- `gen_ai.usage.input_tokens` → 输入多大(上下文长度)
- `gen_ai.usage.cache_read_input_tokens` → 缓存命中多少

对比第一个和最后一个 `chat` span 的 input_tokens — 如果增长了,说明工具结果
被加进了上下文(正常);如果暴增,可能 governance 没截断。

### Step 4:查工具执行

找到 `execute_tool` span:

- `gen_ai.tool.name` → 什么工具
- `gen_ai.tool.success` → 成功吗
- 失败了看 `gen_ai.tool.error_type` 和 result 里的错误信息

### Step 5:看 Scores

trace 详情页底部或 Scores 区域,看 12 个指标:

- `tool_success_rate` 低?→ 查失败的 execute_tool
- `iteration_count` 高?→ 看 chat span 里 LLM 在干什么(是否在打转)
- `cache_hit_rate` 低?→ 检查系统提示是否在变(动态注入导致缓存失效)
- `api_latency_avg_s` 高?→ 可能是慢模型或网络,考虑换模型

### Step 6:对比 session 趋势

如果这个 turn 是一个对话的第 5 轮,用 `session_id` 筛选同 session 的所有 trace,
对比每轮的 12 指标变化:

- `total_input_tokens` 应该随对话增长(历史变长)
- `cache_hit_rate` 应该稳定(系统提示不变)
- `iteration_count` 应该波动不大(任务难度相似)

## 8. 典型问题排查

### Agent 在打转(loop detection 没拦住)

**症状:** `iteration_count` 很高(>15),`tool_success_rate` 可能 1.0(工具都成功
但 agent 重复调同样工具)

**查法:** 看 `chat` span 的 `gen_ai.output.tool_calls` — 如果每轮调的工具名
和参数都一样,就是打转。看 `gen_ai.output.messages` 的 content — LLM 在说什么?

### 工具总是失败

**症状:** `tool_success_rate` 低,`error_tool_count` > 0

**查法:** 筛 `name = execute_tool` + `level = ERROR`,看 `gen_ai.tool.error_type`
和 result。常见原因:路径不存在、权限不足、参数格式错。

### LLM 调用太慢

**症状:** `api_latency_avg_s` 高(>10s)

**查法:** 筛 `type = GENERATION`,按延迟降序排。看 `gen_ai.request.model` —
是不是换了慢模型?看 `gen_ai.usage.input_tokens` — 输入太大也会慢。

### 缓存不工作

**症状:** `cache_hit_rate` 低(<0.3)

**查法:** 对比连续两个 `chat` span 的系统提示部分。如果系统提示里被注入了
时间戳、绝对路径、每小时变化的 metadata,缓存就失效了。ModexAgent 的
`RuntimeProvider` 会注入时间 — eval 用 `static_system_prompt` 规避此问题。

### 审批被拒

**症状:** 有 `human.review` span,decision = denied

**查法:** 看 `human.review` 的 attributes — `deny_reason`、触发的 `tool_name`。
看对应的 `execute_tool` span 的参数 — agent 想做什么被拦了。

## 9. v4 注意事项

### 数据延迟

Langfuse v4 events_only mode 异步写入 ClickHouse。ingestion API 返回 207(成功)
后,score 和 trace 可能需要几秒到几十秒才在 UI 可见。如果你刚跑完一个 turn
立刻刷新看不到数据,等 30 秒再试。

### observations 表 vs traces 视图

v4 推荐用 **Observations 表**(更灵活,支持按 span 级筛选)。传统的 Traces
视图仍然可用,但功能较少。本指南的操作都基于 Observations 表。

### Root Observation 过滤

打开 observations 表时默认过滤 `Is Root Observation = true`。如果你看不到数据,
检查左侧筛选栏是否误加了过滤。清除所有过滤看全部数据。

## 10. 速查表

```
一个 trace = 一个 agent turn
一个 session = 一个对话(conversation_id)
一个 observation = 一个 span

invoke_agent     ← 根,看 stop_reason
chat             ← LLM 调用,看 model/tokens/latency/cache
execute_tool     ← 工具,看 success/error
iteration.start  ← ReAct 轮数
human.review     ← 审批决策
agent.handoff    ← 子 agent 派发
training_tag     ← 训练相关性

12 指标:
  工具质量:  tool_success_rate / tool_call_count / error_tool_count
  循环效率:  iteration_count / llm_call_count
  成本:      total_input_tokens / total_output_tokens / total_reasoning_tokens
  性能:      api_latency_avg_s / cache_hit_rate
  辅助:      response_token_ratio / has_reasoning
```
