# Graph 可视化信息架构重构 — 完整设计规划

Status: ready-for-implementation
Labels: graph, visualization, frontend, ia-redesign
Blocking: 无

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
| **Instance 详情** | `GraphInstanceDetail` (NEW) | 会话流 + continue composer + 拓扑小窗 | **类似 chat 会话页** — 对话历史 + 底部输入触发下一次 invocation |

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
│           │  │  spec v1.0                             │                  │ │
│           │  └────────────────────────────────────────┴──────────────────┘ │
│           │  ┌─ Composer (横跨主区宽度) ──────────────────────────────────┐ │
│           │  │ [Trigger a new instance...                        ] [▶ Run] │ │
│           │  └────────────────────────────────────────────────────────────┘ │
└───────────┴──────────────────────────────────────────────────────────────────┘
```

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
- 每行:
  - instance ID(mono, text-base)
  - status badge(running/completed/crashed/...)
  - **version 号**(instance 的 invocation version, 如 `v3` — 当前执行版本)
  - 进度 + 耗时(`2/3 · 12s`)
  - 时间戳
- 点击行 → `navigate("/graphs/instances/:id")` → 进入 instance 详情
- 数据来源: `listInstances(ws, spec_id=specId)` (API 已支持 `?spec_id=` 过滤)

**D. Composer(底部, 横跨主区宽度)**
- 输入框 + Run 按钮
- 触发 `POST /specs/{specId}/run` → 新建 instance → 跳转到 instance 详情
- running 时禁用(防止并发触发同一 spec)

### 3.3 spec_id 变化处理

`GraphSpecEditor` 保存后,`PUT /specs/{id}` 可能返回新 `spec_id`(ADR-0040: 内容变化 = 新 spec_id)。`GraphSpecEditor` 保存成功后:
- 如果返回的 `spec_id` 与当前不同 → `navigate("/graphs/{new_spec_id}")`
- `GraphSpecDetail` 的 URL 始终是当前 spec_id

---

## 4. Instance 详情视图(GraphInstanceDetail)

### 4.1 心智模型

**Instance 详情是对话界面**,不是调度可视化界面。用户在这里:
- 看到每次 invocation 的输入和输出(像 chat 的消息历史)
- 在底部输入触发下一次 invocation(re-invoke,ADR-0040)
- 按需查看拓扑(小窗/抽屉,带版本号)

### 4.2 布局

```
┌─ Sidebar ─┬─ Header (Back · #instance · spec name · spec v1.0 · status) ──┐
│           │                                                  [Topology ▾]   │
│ 会话列表   │                                                                  │
│           │  ┌─ 会话流(主区, flex-1)──────────────────────────────────────┐ │
│ pool      │  │                                                                │ │
│           │  │  v1 · 14:32:05                                                 │ │
│ workspace │  │                          [Review the code in PR #42]  → user  │ │
│           │  │  ← graph  I've completed the review. Key findings:            │ │
│ [Graphs]  │  │           1. Missing return type...                           │ │
│           │  │           [mini-topo 3/3 · 45s]                                │ │
│           │  │                                                                │ │
│           │  │  v2 · 14:35:12                                                 │ │
│           │  │                    [Check test coverage for these changes]  → │ │
│           │  │  ← graph  Coverage at 78%. Missing paths in...                 │ │
│           │  │           [mini-topo 3/3 · 38s]                                │ │
│           │  │                                                                │ │
│           │  │  v3 · 14:38:01  ●●● (running)                                 │ │
│           │  │              [Generate summary report and GitHub comment]  →  │ │
│           │  │  ← graph  [running · typing dots · 2/3 · 12s]                 │ │
│           │  │                                                                │ │
│           │  └────────────────────────────────────────────────────────────────┘ │
│           │  ┌─ Composer (底部) ─────────────────────────────────────────────┐ │
│           │  │ [Re-invoke this instance with new input...]        [▶ Invoke]  │ │
│           │  └────────────────────────────────────────────────────────────────┘ │
└───────────┴──────────────────────────────────────────────────────────────────────┘
```

### 4.3 区域分解

**A. 顶部 Header**
- Back 按钮 → `navigate("/graphs/:specId")`(回到 spec 详情)
- instance ID(mono, text-base)
- spec name(text-sm, mute) + spec version badge(ember 色, 标注 instance 绑定的 spec 版本)
- instance status badge
- "Topology" 按钮 → 点击弹出右侧抽屉(见 D)

**B. 会话流(主区, flex-1)**
- 每次 invocation = 一组:
  - version 号 + 时间戳(`v1 · 14:32:05`)
  - user 输入气泡(右对齐, brand tint)
  - graph 输出气泡(左对齐, elevated surface + brand left border)
    - 输出文本(Markdown 渲染)
    - 内嵌 MiniTopology(80×24px, 状态着色) + 进度(`3/3 · 45s`)
- running 中的 invocation:
  - typing dots + status badge + MiniTopology(部分节点 completed)
- 数据来源: `GraphIORecordStore.list_by_instance(instanceId)` — 按 version 排序的 I/O 记录
- 每次 invocation 的节点状态: `getInstance(instanceId)` 的 `nodes` 数组(最新 invocation 的节点状态)

**C. Composer(底部)**
- 输入框 + Invoke 按钮
- 触发 `POST /instances/{instanceId}/invoke` (ADR-0040 re-invocation)
- **终态时可用**(completed/crashed/failed/stopped),running/paused 时禁用
- placeholder: "Re-invoke this instance with new input..."
- 成功后:新 invocation 出现在会话流底部(乐观更新 + 轮询/WS 更新)

**D. 拓扑抽屉(header "Topology" 按钮 → slide-in panel, w=360)**
- instance 绑定的 spec 版本拓扑(带版本号标注)
- scheduler / trigger / nodes 数量 / invocation 版本数
- 节点状态着色(最新 invocation 的节点状态)
- 按 X 或 Esc 关闭
- **不占主区空间** — 对话流全宽

### 4.4 数据流

```
GET /api/graphs/instances/{id}          ← instance metadata + nodes (最新 invocation)
    │
    ▼
GET /api/graphs/specs/{instance.spec_id} ← instance 绑定的 spec (不可变快照, ADR-0040)
    │                                       parseGraphSpecYaml → topology
    ▼
GET /api/graphs/instances/{id}/events   ← WS subscribe_graph (Phase 2) 或 2s 轮询 (Phase 1)
    │
    ▼
会话流更新 + 拓扑抽屉节点状态更新

invocation 历史:
  list_by_instance(instanceId) → [GraphIORecord(v1, input, output), (v2, ...), ...]
  每次 invocation 的输入/输出独立可查 (ADR-0040 change 2)
```

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
| `GraphExecutionViewer` (作为 instance 详情主体) | `GraphInstanceDetail` | 调度可视化是调试用途,非会话心智;全画布拓扑+侧栏与"对话+composer"矛盾 |

### 5.2 保留

| 组件 | 保留原因 |
|------|---------|
| `GraphSpecListPage` | spec 列表正确,MiniTopology 缩略图 + 元信息 |
| `GraphSpecEditor` | YAML 编辑 + 实时预览完整;从 spec 详情 Header 进入 |
| `TopologyCanvas` / `GraphNode` / `GraphEdge` / `DeliverPulse` / `ActiveNodeRing` / `MiniTopology` | 拓扑组件族完整;在 spec 详情(预览)和 instance 详情(抽屉)中复用 |
| `useGraphExecution` hook | 轮询/WS + diff 逻辑;instance 详情会话流复用 |
| `GraphInstanceListPage` 组件代码 | 可复用为 spec 详情右侧 instance 列表的基础(改数据源为 `?spec_id=` 过滤) |

### 5.3 `GraphExecutionViewer` 的去向

`GraphExecutionViewer`(全画布拓扑 + 节点详情侧栏 + 事件时间线 + deliver 面板)**不废弃,但降级为 instance 详情拓扑抽屉内的"展开查看"二级视图**。用户在拓扑抽屉中看到概览后,可点击"查看调度详情"展开 `GraphExecutionViewer` 的全画布调度可视化(deliver 脉冲/节点详情/事件时间线)。

这是"调试单次执行的调度过程"用途 — 不是日常使用路径,但保留给需要深入分析的用户。

---

## 6. API 变更

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

## 7. 实现分期

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

### Phase 2 — 实时事件 + 调度可视化降级

- `GraphInstanceDetail` 拓扑抽屉内嵌 `GraphExecutionViewer`(全画布调度可视化作为二级视图)
- WS `subscribe_graph` 驱动会话流实时更新(替代轮询)
- `useGraphExecution` WS 模式集成

---

## 8. 设计系统约束

所有新增组件遵循 "Teal & Ember Console" 设计系统(记录在 `webui/AGENTS.md`):
- CSS 变量 token(`:root` / `.dark`),不硬编码颜色
- Inter + JetBrains Mono 字体
- 复用已有组件: `Button`, `IconButton`, `SectionLabel`, `DropdownPanel`, `Card`, `MarkdownRenderer`
- 消息气泡: `.bubble-user` / `.bubble-assistant`(已有 CSS class)
- Composer: `.composer`(已有 CSS class,带 brand focus glow)
- 状态 badge: `GraphStatusBadge`(已有)
- 拓扑组件: `TopologyCanvas` / `GraphNode` / `GraphEdge` / `MiniTopology` / `DeliverPulse` / `ActiveNodeRing`(已有)
- i18n: 所有文案走 `useT()` + `MessageKey`
- `prefers-reduced-motion` 降级
