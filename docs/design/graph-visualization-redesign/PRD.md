# Graph 可视化信息架构重构 — 完整设计规划

Status: ready-for-implementation
Labels: graph, visualization, frontend, ia-redesign
Blocking: 无

> **Rev 3(2026-08-14,原型评审后定稿)**:原型 `#/prototype/graph/A–D`(dev-only)评审结论 =
> **Variant A 的图体验入选,但页面布局回归会话优先**。据此:
> - §4 实例详情主区为**会话流**,运行图默认不显示;header 的 Topology 按钮点击后以**居中弹窗
>   (Run Graph modal)**打开,弹窗内承载 Variant A 完整体验(大画布 + 状态着色 + 脉冲 +
>   事件时间线 + 节点详情 + Pause/Resume/Stop/Deliver);原 360px 右抽屉 `TopologyDrawer` 移除;
> - §3 spec 详情:底部输入 + Run 替换为 **FAB(＋)→ New Instance 居中弹窗输入框**(原 composer
>   独立版,类似新建会话),提交后跳转实例详情;Instances 列表行重设计;
> - §6 状态视觉系统**保留不变**;
> - 全部新增 UI 文案 i18n 英文(现仅有英文 locale);
> - 原型代码归档于 throwaway 分支 `prototype/graph-redesign`(commit `60a05666`),主分支不保留
>   (仅 `--color-graph-status-*` token 与 `index.tokens.test.ts` 用例作为 T10 交付物留在主分支)。
>
> Rev 2 的 §4/T09 决策("活图主视图")被 Rev 3 再次修订;§6 保留。

> **Rev 2(2026-08-14)**:用户反馈实例详情"看不到 graphInstance 的动态流程,只看得到静态图;图例状态颜色区分度低"。据此:
> - §4 实例详情改为**活图主视图**(方案 A):原 `GraphExecutionViewer` 布局转正,与会话流/re-invoke composer 合并为单一组件;
> - §5.3 Rev 1 的"GraphExecutionViewer 降级为二级视图"决策被推翻(T07 废弃);
> - 新增 **§6 状态视觉系统**(图例着色、graph-status 专用 token、节点整节点双通道着色、badge 升级);
> - 实现分期追加 **Phase 3**(tickets T09–T12)。

## 0. 文档定位

本文档是 Graph 可视化信息架构重构的**完整设计规划**,覆盖信息架构、视图设计、交互模式、数据流和实现分期。实现 ticket 在同目录 `tickets.md`。

前置文档:
- `docs/adr/0040-graph-instance-re-invocation-and-iorecord-version-scoping.md` — re-invocation + spec 不可变(权威决策)
- `docs/design/graph-orchestration/` — 图调度子系统设计(已闭合)
- `examples/bot_project/webui/AGENTS.md` — 设计系统 "Teal & Ember Console" 记录

---

## 1. 问题诊断

### 1.1 当前信息架构错位

| 路由 | 当前组件 | 问题 |
|------|---------|------|
| `/graphs` | `GraphSpecListPage` | ✅ 正确 — spec 列表 |
| `/graphs/:specId` | `GraphConversation` | ❌ 伪会话 — chat bubble 堆叠 runGraph 调用,无拓扑 |
| `/graphs/:specId/edit` | `GraphSpecEditor` | ✅ 正确 — YAML 编辑 + 拓扑预览 |
| `/graphs/instances` | `GraphInstanceListPage` | ❌ 全局扁平列表 — instance 无 spec 归属 |
| `/graphs/instances/:id` | `GraphExecutionViewer` | ❌ 调度可视化为主体 — 非会话心智 |

### 1.2 核心问题

1. **schema 点开看到会话内容**: `/graphs/:specId` 渲染 `GraphConversation`(chat bubbles),而非 spec 拓扑结构 + instance 列表
2. **instance 列表无层级**: `/graphs/instances` 是跨 spec 的全局扁平列表,不从属于 spec
3. **每次发送新建 instance**: `GraphConversation.handleSend` 调 `runGraph`(新建 instance),而非继续调用现有 instance
4. **调度可视化是主体**: `GraphExecutionViewer`(全画布拓扑 + 侧栏)是调试视图,不是会话视图 — 用户期望"对话 + 底部输入"心智

### 1.3 spec/instance 版本关系(ADR-0040)

spec 不可变(ADR-0040 change 3):每次内容变化 = 新 `spec_id`(Snowflake)。`GraphInstance.spec_id` 是内容快照引用 — 指向 instance 创建时的 spec,不跟随 spec 修改。`list_records()` 只返回每个 name 的最新 `spec_id`;历史 spec 只能通过 `spec_id` 访问(被 instance 引用)。

---

## 2. 目标信息架构

### 2.1 路由

```
/graphs                              → GraphSpecListPage     (spec 列表, 当前正确)
  └─ /graphs/:specId                 → GraphSpecDetail       (NEW — 替换 GraphConversation)
       ├─ /graphs/:specId/edit       → GraphSpecEditor       (YAML 编辑, 保留)
       └─ /graphs/instances/:id      → GraphInstanceDetail   (NEW — 替换 GraphExecutionViewer)

废弃路由:
  /graphs/instances                  → GraphInstanceListPage (废弃 — instance 从属于 spec)
  /graphs/:specId (GraphConversation)→ 废弃, 替换为 GraphSpecDetail
```

### 2.2 全局布局约束

**左侧 Sidebar(会话管理列)在所有 graph 视图中保留** — graph 视图不覆盖 Sidebar,而是像 chat/settings 一样在 Sidebar 右侧的主区渲染。Sidebar 包含:会话列表、pool 选择器、workspace 指示器、"Graphs" 入口按钮。

### 2.3 视图总览

| 视图 | 组件 | 职责 | 心智模型 |
|------|------|------|---------|
| Spec 列表 | `GraphSpecListPage` | 浏览工作流 spec | 列表(当前正确) |
| **Spec 详情** | `GraphSpecDetail` (NEW) | 拓扑预览 + instance 列表 + 新建 instance | 类似"项目详情页" — 看结构 + 看历史运行 + 触发新运行 |
| Spec 编辑 | `GraphSpecEditor` | YAML 编辑 + 实时预览 | 编辑器(保留) |
| **Instance 详情** | `GraphInstanceDetail`(Rev 3 会话优先) | 会话流 + re-invoke composer + 运行图弹窗(按需) | 类似 chat 会话页 — 活图按需弹窗查看 |

---

## 3. Spec 详情视图(GraphSpecDetail)

### 3.1 布局

```
┌─ Sidebar ─┬─ Header (Back · spec name · version · Edit YAML) ──────────────┐
│           │                                                                  │
│ 会话列表   │  ┌─ 拓扑预览(主区, flex-1)──────────────┬─ Instance 列表 ──┐ │
│           │  │                                        │ w=320            │ │
│ pool      │  │     [START]                            │                  │ │
│ 选择器    │  │       │                                │ #12347 running   │ │
│           │  │     [designer]                        │   v3 · 2/3 · 12s │ │
│ workspace │  │       │                                │ #12346 completed │ │
│           │  │     [implementer] ←─ loop             │   v2 · 3/3 · 45s │ │
│ [Graphs]  │  │       │     ↺                          │ #12345 completed │ │
│           │  │     [reviewer]                        │   v1 · 3/3 · 38s │ │
│           │  │       │                                │ #12344 crashed   │ │
│           │  │     [END]                              │   — · 1/3 · 8s   │ │
│           │  │                                        │                  │ │
│           │  │  3 nodes · parallel · on_all_preds     │                  │ │
│           │  │  spec v1.0                      [＋ FAB] │                  │ │
│           │  └────────────────────────────────────────┴──────────────────┘ │
└───────────┴──────────────────────────────────────────────────────────────────┘
```

**Rev 3 变更**:~~底部横跨主区的 composer~~(Rev 1)与 ~~FAB → hero 输入态~~(Rev 2)均移除。
新建实例 = 右下角 **FAB(＋)→ New Instance 居中弹窗输入框**(原 composer 的独立版,类似会话
"新建会话"能力):提交 `runGraph` 后跳转 instance 详情。弹窗规格见 §3.2 D;Instances 列表行
重设计见 §3.2 C。

### 3.2 区域分解

**A. 顶部 Header**
- Back 按钮 → `navigate("/graphs")`
- spec name(Inter 600, text-md)
- spec version(mono, text-xs, faint) — 当前 spec 的 version 标签
- "Edit YAML" 按钮 → `navigate("/graphs/:specId/edit")`

**B. 拓扑预览(主区, flex-1)**
- 全画布 SVG 拓扑(dagre TB 布局),复用 `TopologyCanvas`(无状态着色 — 纯结构展示)
- 底部标注:节点数 · scheduler · trigger · spec version
- 可缩放/拖拽(复用 TopologyCanvas 交互)
- **拓扑是主体视觉** — 用户点开 spec 首先看到图结构

**C. Instance 列表(右侧, w=320)**
- 该 spec 的所有 instance,按创建时间倒序
- **行重设计(Rev 3)**,每行:
  - `#id`(mono)
  - 升级版彩色 status badge(§6.4)
  - 进度/耗时(`2/3 nodes · 12s`)
  - 相对时间
  - MiniTopology(带节点状态着色;数据已在 `GraphSpecDetail` 拉取的 `nodeStatuses` map)
- hover/active 态与 Sidebar 会话行一致(`--color-session-hover/active` token)
- 点击行 → `navigate("/graphs/instances/:id")` → 进入 instance 详情
- 数据来源: `listInstances(ws, spec_id=specId)` (API 已支持 `?spec_id=` 过滤)

**D. 新建实例(Rev 3:FAB + New Instance 居中弹窗,替换原底部 composer)**
- **FAB**:主区右下角悬浮圆形按钮(＋ icon,56px 圆形 primary,绝对定位于拓扑预览区右下角
  `right-6 bottom-6`,z-index 高于画布但不遮挡图例);只负责"开启新建",本身不带输入
- **New Instance modal**:点击 FAB 打开居中弹窗输入框 — 原 composer 的独立版,类似会话
  "新建会话"能力:
  - 弹窗内容:spec name + version 标签 + `.composer` 风格 textarea(autofocus;Enter = Run,
    Shift+Enter 换行)+ Run 按钮
  - 提交:`POST /specs/{specId}/run`(`runGraph`)→ 新建 instance →
    `navigate("/graphs/instances/:id")`(与现行行为一致)
  - 取消:Esc / ✕ / 点击 backdrop;提交中禁用输入与按钮;错误在弹窗内展示
- a11y:FAB 带 `aria-label`;弹窗 textarea autofocus;Esc 关闭符合全局 modal 惯例

### 3.3 spec_id 变化处理

`GraphSpecEditor` 保存后,`PUT /specs/{id}` 可能返回新 `spec_id`(ADR-0040: 内容变化 = 新 spec_id)。`GraphSpecEditor` 保存成功后:
- 如果返回的 `spec_id` 与当前不同 → `navigate("/graphs/{new_spec_id}")`
- `GraphSpecDetail` 的 URL 始终是当前 spec_id

---

## 4. Instance 详情视图(GraphInstanceDetail,Rev 3 会话优先 + 运行图弹窗)

### 4.1 心智模型(Rev 3 修订)

**Instance 详情是会话界面,运行图按需弹窗查看。** Rev 2 曾将全画布活图转正为页面主体;
Rev 3 原型评审(`#/prototype/graph/A–D`)再次修订 — Variant A 的图体验入选,但评审反馈
"默认应聚焦会话",页面布局回归**会话优先**。用户在这里:

- **主区浏览 invocation 会话流**:每次 invocation 的 user/输出气泡 + 内嵌 MiniTopology,
  类似 chat 会话页
- **底部触发下一次 invocation**:re-invoke composer 常显(ADR-0040)
- **按需打开运行图**:header 的 Topology 按钮点击后以居中弹窗(Run Graph modal)打开,
  弹窗内承载 Variant A 完整体验 — 大画布 + 状态着色 + 脉冲 + 事件时间线 + 节点详情 +
  Pause/Resume/Stop/Deliver 控制;运行图默认不显示
- 原 360px 右抽屉 `TopologyDrawer` 移除(Rev 1 遗留,被 modal 替代)

### 4.2 布局(Rev 3)

现产线会话布局保留,仅 Topology 按钮行为变更(360px 右抽屉 → Run Graph modal):

```
┌─ Sidebar ─┬─ Header (Back · #id · spec name · version · status · [Topology]) ─────────────┐
│           ├──────────────────────────────────────────────────────────────────────────────┤
│ 会话列表   │  会话流 (主区, flex-1)                                                       │
│           │    v3 · 14:32                                                                │
│ pool      │    ▐ user: 生成实现计划                                                       │
│ 选择器    │    ▌ graph 输出 (MarkdownRenderer) + MiniTopology(状态着色)                   │
│           │    v2 · 14:20                                                                │
│ workspace │    ▐ user: ...                                                               │
│           │    ▌ graph 输出 (MarkdownRenderer) + MiniTopology(状态着色)                   │
│ [Graphs]  │                                                                              │
│           ├──────────────────────────────────────────────────────────────────────────────┤
│           │ Composer: [Re-invoke this instance with new input...]              [▶ Invoke] │
└───────────┴──────────────────────────────────────────────────────────────────────────────┘
```

点击 header `[Topology]` 后,Run Graph modal 以覆盖层打开(默认不显示,不自动打开):

```
┌─ Sidebar ─┬─ 页面 (header + 会话流 + composer, backdrop 变暗) ──────────────────────────┐
│           │   ┌─ Run Graph modal (居中近全屏: inset-6 · max-w 1200 · 高 85vh) ───────┐ │
│           │   │ spec name · v3 · [status]          [Pause] [Resume] [Stop]       [✕] │ │
│           │   ├─────────────────────────────────────────┬─ 右栏 (w-80) ──────────────┤ │
│           │   │                                         │ 节点详情 / 实例摘要         │ │
│           │   │          全尺寸 TopologyCanvas          ├────────────────────────────┤ │
│           │   │   [START]                               │ EventTimeline (max-h 35%)  │ │
│           │   │     │                                   ├────────────────────────────┤ │
│           │   │  [designer] ← 呼吸环 + 脉冲              │ Deliver 面板               │ │
│           │   │     │                                   │ (running/paused 时)        │ │
│           │   │  [END]        右上: 彩色图例(§6.3)       │                            │ │
│           │   └─────────────────────────────────────────┴────────────────────────────┘ │
│           │       Esc / ✕ / backdrop 关闭 · 焦点返还 [Topology] 按钮                    │
└───────────┴──────────────────────────────────────────────────────────────────────────────┘
```

小屏(≤768px):modal 内右栏下移堆叠于画布下方。

### 4.3 区域分解(Rev 3)

**A. 顶部 Header(现产线不变;Topology 按钮行为变更)**
- Back 按钮 → `navigate("/graphs/:specId")`(回到 spec 详情)
- instance ID(mono)+ spec name + spec version badge(标注 instance 绑定的 spec 版本)
- instance status badge(§6.4,升级后带底色)
- **Topology 按钮** → 打开 Run Graph modal(Rev 3:不再开 360px 右抽屉;`TopologyDrawer` 删除)

**B. 会话流(主区, flex-1)— 现产线不变**
- 每次 invocation = version 号 + 时间戳 + user 气泡(`.bubble-user`)+ graph 输出气泡
  (`.bubble-assistant` + MarkdownRenderer + MiniTopology)
- running invocation:typing dots + MiniTopology 实时着色(§6 新状态色)

**C. re-invoke Composer(底部)— 现产线不变**
- `POST /instances/{id}/invoke`(ADR-0040 re-invocation),终态可用、running/paused 禁用;
  乐观更新 + WS/轮询收敛

**D. Run Graph modal(Rev 3 新增;承载 Variant A 完整体验)**
- **尺寸/层级**:近全屏居中弹窗(如 `inset-6` / `max-w-[1200px]`,高 85vh),
  `bg-canvas-popover` + `shadow-card-hover`;默认不显示,不自动打开
- **a11y**:`role="dialog"` + `aria-modal` + focus trap;Esc / ✕ / backdrop 关闭,关闭后
  焦点返还 Topology 按钮
- **顶栏**:spec name · version + 状态 badge(§6.4)+ Pause/Resume/Stop 控制组(复用
  `GraphExecutionViewer` 的 `canPause`/`canResume`/`canStop` 状态机,操作后 `refresh()`)+ ✕
- **主区**:全尺寸 `TopologyCanvas` — `nodeStatuses` + `activeEdges` + `pulses` + crash flash
  全量接入;running 呼吸环、completed/crashed 整节点双通道着色(§6.2);右上角彩色图例(§6.3);
  agent 节点单击 → 跳转该节点 session;非 agent 节点单击 → 选中,右栏切到节点详情
- **右栏(w-80)**:选中节点 → `NodeDetailPanel`(类型/pool/状态/result/打开 session);
  默认 → `InstanceSummary`(spec name/version、scheduler、trigger、进度环、graph 级 result);
  下方 `EventTimeline`(max-h 35%,接入 `useGraphExecution` 的 `timeline` — 当前产线未消费);
  running/paused 时 inline Deliver 面板(`DropdownPanel` + textarea + Send)
- **小屏(≤768px)**:右栏下移堆叠于画布下方

### 4.4 数据流

```
GET /api/graphs/instances/{id}          ← instance metadata + nodes (最新 invocation)
    │
    ▼
GET /api/graphs/specs/{instance.spec_id} ← instance 绑定的 spec (不可变快照, ADR-0040)
    │                                       parseGraphSpecYaml → topology → layoutGraph
    ▼
WS subscribe_graph (首选, G11 已实现)     ← graph_event: node_started/node_completed/
    │                                       node_crashed/deliver_dispatched/graph_completed
    ▼                                       (断线自动回退 2s 轮询, 重连重订阅)
画布节点状态 + 脉冲 + 事件时间线 + 侧栏 实时更新

invocation 历史:
  getInvocations(instanceId) → [GraphIORecord(v1, input, output), (v2, ...), ...]
  instance 进入终态时刷新一次 (终态后不再轮询)
```

Run Graph modal 打开期间 WS/轮询照常驱动(modal 内画布/时间线实时刷新);关闭后页面的
状态 badge 与气泡内进度继续实时更新 — 数据流不随 modal 开合中断。

### 4.5 re-invoke 流程

```
用户在 composer 输入 → 点击 Invoke
    │
    ▼
POST /api/graphs/instances/{id}/invoke  (body: { user_input: { content: "..." } })
    │                                       → orch.start_invoke(gid, user_input)
    │                                       → begin_invocation (version N+1)
    │                                       → bootstrap → [entry_node] (ADR-0040 change 1)
    │                                       → 新 GraphIORecord (version N+1)
    ▼
乐观更新: 会话流底部添加新 invocation (pending)
    │
    ▼
WS subscribe_graph / 2s 轮询 → 节点状态更新 → MiniTopology 着色
    │
    ▼
graph_completed → 输出文本渲染到输出气泡 → composer 解禁
```

---

## 5. 废弃组件与路由

### 5.1 废弃

| 组件/路由 | 替换 | 原因 |
|-----------|------|------|
| `GraphConversation` | `GraphSpecDetail` + `GraphInstanceDetail` | chat bubble 不适合 graph DAG;伪会话语义误导;ADR-0040 明确要求替换 |
| `/graphs/instances` (全局列表) | spec 详情右侧 instance 列表 | instance 从属于 spec;全局扁平列表无层级 |
| `TopologyDrawer`(360px 右抽屉)(Rev 3) | Run Graph modal(§4.3 D) | 360px 装不下 Variant A 完整体验;默认关闭使活图不可见;Rev 3 以近全屏弹窗承载大画布 + 控制组 + 侧栏 |
| `GraphExecutionViewer` 作为独立组件(Rev 3) | 控制状态机/侧栏/Deliver/elapsed 迁移入 Run Graph modal(§4.3 D);迁移完成后独立组件与测试删除 | Rev 1 曾将其降级为抽屉内二级视图(T07);Rev 2 曾转正为 instance 详情主体;Rev 3 回归会话优先 — 体验完整迁移进 modal |
| Rev 1 版 `GraphInstanceDetail`(会话流为主体 + 拓扑抽屉) | Rev 3 版:会话流主体保留,抽屉升级为 Run Graph modal(§4) | Rev 2 曾整体替换为活图主视图;Rev 3 评审后回归会话优先,运行图按需弹窗查看 |

### 5.2 保留

| 组件 | 保留原因 |
|------|---------|
| `GraphSpecListPage` | spec 列表正确,MiniTopology 缩略图 + 元信息 |
| `GraphSpecEditor` | YAML 编辑 + 实时预览完整;从 spec 详情 Header 进入 |
| `TopologyCanvas` / `GraphNode` / `GraphEdge` / `DeliverPulse` / `ActiveNodeRing` / `MiniTopology` | 拓扑组件族完整;在 spec 详情(预览)、instance 详情会话流(MiniTopology)与 Run Graph modal(全尺寸画布)中复用 |
| `useGraphExecution` hook | 轮询/WS + diff 逻辑;instance 详情会话流复用 |
| `GraphInstanceListPage` 组件代码 | 可复用为 spec 详情右侧 instance 列表的基础(改数据源为 `?spec_id=` 过滤) |

### 5.3 `GraphExecutionViewer` 的去向(Rev 3 修订)

Rev 1 曾将 `GraphExecutionViewer`(全画布拓扑 + 节点详情侧栏 + 事件时间线 + deliver 面板)降级为 instance 详情拓扑抽屉内的"展开查看"二级视图(T07,未实现,组件从未接线)。Rev 2 推翻该决策:全画布布局"转正为 instance 详情主体"(§4 Rev 2 活图主视图)。**Rev 3 再次修订**:原型评审结论 = Variant A 图体验入选、页面布局回归会话优先 — Rev 2 的"转正为主体"表述更正为**"转正为 modal 内体验"**。

Rev 3 决定:`GraphExecutionViewer` 的完整体验 — 控制状态机(Pause/Resume/Stop)、全尺寸画布、上下文侧栏(节点详情/实例摘要)、`EventTimeline`、inline Deliver 面板、elapsed 计时 — **迁移进 Run Graph modal**(§4.3 D,T09);迁移完成后组件本体 `GraphExecutionViewer.tsx` 与其测试删除,不作为独立组件存在。T07 保持废弃。

---

## 6. 状态视觉系统(Rev 2 新增)

### 6.1 问题

现状状态色有三处硬伤:

1. **图例零着色**:`TopologyCanvas` 右上角图例整行 `text-faint` 单色渲染
   (`● completed · ◎ running · ● crashed · ○ pending`),且 completed 与 crashed 连用同一
   个 `●` 字形 — 用户无法建立"颜色 → 状态"的映射。
2. **completed 与 running 撞色**:dark 主题下 `--color-success: var(--color-brand)`(#2DD4BF),
   running(brand 描边)与 completed(success 点)是同一 teal 色相,只能靠"空心点 vs 实心点"
   这种弱通道区分。
3. **状态通道太弱**:状态只体现在 8px 小圆点上,节点本体(fill/border)几乎不随状态变化;
   pending 与 canceled 同为灰色系(canceled 仅 45% 透明度差异)。

### 6.2 graph-status 专用 token

新增 `--color-graph-status-*` token 族(`index.css` `:root` / `.dark` 双主题),**不改动全局
`--color-success`/`--color-warning`**(避免影响全局语义色用法;graph 域自带色相):

| 状态 | token | dark | light |
|------|-------|------|-------|
| pending | `--color-graph-status-pending` | `--color-mute` | `--color-mute` |
| running | `--color-graph-status-running` | brand `#2DD4BF` | brand `#0D9488` |
| completed | `--color-graph-status-completed` | 独立绿 `#34D399` | `#059669` |
| crashed / failed | `--color-graph-status-crashed` | danger `#F87171` | `#DC2626` |
| suspended / paused | `--color-graph-status-suspended` | warning `#FBBF24` | `#B45309` |
| canceled / stopped | `--color-graph-status-canceled` | mute 45% | mute 45% |

completed 的独立绿与 running 的 teal 拉开约 40° 色相,两个"进行中/已完成"的高频相邻状态
不再撞色。

### 6.3 节点与图例

**节点整节点双通道着色**(不再只靠 8px 状态点):

| 状态 | dot | 节点本体 |
|------|-----|---------|
| pending | 实心 mute | 默认 fill + hairline 描边 |
| running | 空心 brand 描边点 | brand 描边 + 呼吸环(ActiveNodeRing)+ fill 不变 |
| completed | 实心绿 | 绿描边 + 绿 tint 底色(`color-mix(status 18%)`,沿用现有双通道模式) |
| crashed | 实心红 | 红描边 + 红 tint 底色 |
| suspended | 实心琥珀 | 琥珀虚线描边(保留现有 dashed 通道) |
| canceled | 实心 45% mute | 默认 fill + hairline 描边 + **名称删除线** |

**图例彩色 chip 化**:每个状态一项 = 8px 实心圆点(真实状态色)+ 文字标签(`text-body`,
不再整行 `text-faint`);crashed 用 `✕` 字形与 completed 区分;补充 suspended/canceled 两项
(当前图例只有 4 项,与实际 6 态不符)。

### 6.4 GraphStatusBadge 升级

纯描边文字 chip → **带底色 chip**:`bg = color-mix(status 12%)`,`text/border = status 色`
(border 用 35% mix)。pending/stopped 拉开灰度层次(pending = mute 边框 + 文字,
stopped = faint + 删除线)。复用同一 `--color-graph-status-*` token,不另起色系。

### 6.5 同步面

- `MiniTopology` 的 `MINI_STATUS_FILL` 切到新 token(completed 绿、running brand,保持同步)。
- `GraphSpecInstanceRow` / 实例列表 badge 复用升级后的 `GraphStatusBadge`。
- 全部状态着色遵守 `prefers-reduced-motion` 降级:呼吸环/脉冲动画关闭时,双通道静态着色
  仍保证可分辨(这正是双通道设计的目的)。

---

## 7. API 变更

### 6.1 新增

| 端点 | 方法 | 用途 |
|------|------|------|
| `POST /instances/{id}/invoke` | 已存在(G09/G10) | re-invoke instance(ADR-0040) |
| `listInstances?spec_id=X` | 已存在 | 按 spec 过滤 instance(spec 详情右侧列表) |

### 6.2 前端 API client 新增

```typescript
// graphsApi.ts
export async function invokeInstance(
  workspaceId: string,
  instanceId: string,
  userInput?: string,
): Promise<GraphRunResponse> {
  // POST /instances/{id}/invoke
}

// updateSpec 返回类型已携带 spec_id — 无需新增, 前端需检查 spec_id 是否变化
```

### 6.3 不变

所有 graph REST 端点保持不变。`getSpec(specId)` 对历史 spec_id 有效(ADR-0040: spec 不可变,旧 spec_id 内容保留)。`getRuns(specId)` 保留(spec 详情可用,返回该 spec 的 IORecord 列表)。

---

## 8. 实现分期

### Phase 1 — 后端 spec 不可变 + 前端信息架构

**后端**:
- `GraphSpecStore`: `save` → INSERT;`save_if_changed`;`list_records` → latest per name
- `graph_specs` DDL: 移除 `UNIQUE(name, version)` + 移除 auto-updated_at trigger
- `handle_put_spec`: 改用 `save_if_changed`,移除 name/version 不可变检查
- `GraphSpecLoader`: 改用 `save_if_changed`

**前端**:
- `GraphSpecDetail` 新组件(拓扑预览 + instance 列表 + composer)
- `GraphInstanceDetail` 新组件(会话流 + composer + 拓扑抽屉)
- 路由调整(`/graphs/:specId` → `GraphSpecDetail`;`/graphs/instances/:id` → `GraphInstanceDetail`)
- `GraphConversation` 废弃
- `/graphs/instances` 全局路由废弃
- `graphsApi.ts` 新增 `invokeInstance`
- `GraphSpecEditor` 保存后 spec_id 变化导航

### Phase 2 — 实时事件 + 调度可视化降级(Rev 2 修订)

- ~~`GraphInstanceDetail` 拓扑抽屉内嵌 `GraphExecutionViewer`~~ → Rev 2 废弃(T07 废弃,见 §5.3)
- WS `subscribe_graph` 驱动实时更新(替代轮询)— 已落地(`useGraphExecution` WS 模式)
- `useGraphExecution` WS 模式集成 — 已落地

### Phase 3 — 运行图弹窗 + 状态视觉系统(Rev 3 修订)

- **T09**:Instance 详情运行图弹窗 — Topology 按钮 → Run Graph modal(§4.3 D:顶栏控制组 +
  全尺寸 TopologyCanvas + 右栏节点详情/实例摘要 + EventTimeline + Deliver 面板);
  `TopologyDrawer` 删除;`GraphExecutionViewer` 体验迁移入 modal 后组件与测试删除
- **T10**:状态视觉系统(§6)— graph-status tokens(已随原型入库 `index.css`)+ tailwind
  映射 + 图例彩色 chip + 节点整节点双通道着色 + `GraphStatusBadge` 带底色 + `MiniTopology` 同步
- **T11**:Spec 详情新建实例 — 移除底部 composer;FAB(＋)→ New Instance modal(§3.2 D);
  Instances 列表行重设计(§3.2 C)
- **T12**:收尾 — a11y(modal focus trap/Esc)、reduced-motion 全量降级、双主题视觉 sweep、
  `webui/AGENTS.md` graph 组件表更新
- 执行顺序:T09 + T10 并行 → T11 → T12

---

## 9. 设计系统约束

所有新增组件遵循 "Teal & Ember Console" 设计系统(记录在 `webui/AGENTS.md`):
- CSS 变量 token(`:root` / `.dark`),不硬编码颜色
- Inter + JetBrains Mono 字体
- 复用已有组件: `Button`, `IconButton`, `SectionLabel`, `DropdownPanel`, `Card`, `MarkdownRenderer`
- 消息气泡: `.bubble-user` / `.bubble-assistant`(已有 CSS class)
- Composer: `.composer`(已有 CSS class,带 brand focus glow)
- 状态 badge: `GraphStatusBadge`(已有)
- 拓扑组件: `TopologyCanvas` / `GraphNode` / `GraphEdge` / `MiniTopology` / `DeliverPulse` / `ActiveNodeRing`(已有)
- 状态色: graph 域专用 `--color-graph-status-*` token(§6.2),不改全局 `--color-success`/`--color-warning`
- i18n: 所有文案走 `useT()` + `MessageKey`
- `prefers-reduced-motion` 降级
