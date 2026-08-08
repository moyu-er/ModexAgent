# Graph 可视化与调度过程展示 — 完整设计规划

Status: implemented (Phase 1 + Phase 2) — Rev 2 评审修订(见附录 C)
Labels: graph, visualization, frontend
Blocking: 无(设计文档, 待评审)

## 0. 文档定位

本文档是 Graph 可视化重构的**完整设计规划**,覆盖视觉语言、信息架构、交互模式、数据流、动效规范和分期策略。实现阶段以此文档为唯一设计依据;实现 ticket 后续从本文档拆出。

前置文档:
- `docs/design/static-graph-scheduling/PRD.md` — 图调度子系统设计(已闭合)
- `docs/design/static-graph-scheduling/issues/09-webui-graph-control-api.md` — REST API 设计(已闭合)
- `docs/design/static-graph-scheduling/issues/10-webui-graph-visual-config.md` — 前端原始设计(已闭合,本期超越)
- `docs/design/teal-ember-redesign/DESIGN.md` — "Teal & Ember Console" 设计系统(已落地)

---

## 1. 主体与意图

### 1.1 主体定位

**主体**: AI agent 框架的图调度可视化 — 用户设计由 agent 节点组成的 DAG 工作流(图 spec),触发执行,观察调度过程,控制和干预运行中的图实例。

**受众**: 开发者/高级用户 — 设计 agent 工作流、调试图执行、监控多 agent 协作过程的人。他们理解 DAG、交付(deliver)、调度器(linear/parallel)等概念,需要精确的信息密度和实时反馈。

**单一任务**: 让用户在**一个视觉空间内**完成"理解图结构 → 触发执行 → 观察调度流 → 控制运行 → 检查结果"的完整闭环,而非在扁平列表和原始 YAML 之间来回切换。

### 1.2 当前问题(一句话)

当前实现把图当"列表"渲染 — 节点是扁平 Card 纵向排列,边/拓扑关系完全不可见,YAML 是纯文本框,执行状态是 2 秒轮询的文字徽章。**用户无法看到"图",只能看到"列表"。**

---

## 2. 设计原则(本特性级)

### P1. 图必须看起来像图

拓扑结构(节点 + 边 + 方向)是图 spec 的核心信息,必须被可视化渲染。扁平列表丢失了最关键的"谁连接到谁"的关系。所有涉及图结构的视图都应包含某种形式的拓扑呈现。

### P2. 调度过程必须有"流动感"

执行不只是"状态变了",而是"数据沿着边流动,节点依次激活"。可视化应让这个流动**可见** — 不是静态着色,而是有方向、有节奏的运动。

### P3. 忠于既有设计系统

"Teal & Ember Console" 已落地并成熟。本特性**不引入新调色板、不换字体、不改令牌架构**。所有新增视觉元素从既有 token 派生。唯一的新增是 graph 语义别名(见 §7),且全部映射到已有 color/radius/motion token。

### P4. 一处签名,其余克制

签名元素(deliver 脉冲,见 §4)是唯一的"高调"视觉时刻。其余一切保持 console 的纪律 — 中性画布、hairline 边框、mono 标签、brand 单一彩色线。不堆叠装饰。

### P5. 动效服务信息

所有动画编码真实的调度状态变化,不做纯装饰动效。`prefers-reduced-motion` 降级为静态等效信息(状态着色 + 静态指示器,不丢失信息)。

### P6. 渐进增强

纯前端阶段(Phase 1)用现有 REST 数据 + 客户端 diff 实现完整可视化体验。后端增强(Phase 2)提升实时性和数据精度,但不改变视觉语言。

---

## 3. 信息架构

### 3.1 视图总览

| 视图 | 路由 | 职责 | 核心组件 |
|------|------|------|----------|
| Spec 列表 | `/graphs` | 浏览工作流,快速入口 | `GraphSpecListPage` + `MiniTopology` |
| Spec 编辑器 | `/graphs/:id/edit` | 编辑 YAML + 实时结构预览 + 运行 | `GraphSpecEditor` (分栏) |
| 实例列表 | `/graphs/instances` | 运行历史,状态过滤 | `GraphInstanceListPage` + `MiniTopology` |
| 实例详情 | `/graphs/instances/:id` | **执行查看器(核心视图)** | `GraphExecutionViewer` (全画布拓扑 + 侧栏) |

### 3.2 导航流

```
Sidebar "Graphs" 按钮
    │
    ▼
┌─ Spec 列表 ─────────────────────────────────┐
│  [spec A] → 点击 Edit ──────────────┐       │
│  [spec B]                           │       │
│  [Instances →] ─────────┐           │       │
└─────────────────────────┼───────────┼───────┘
                          │           │
              ┌───────────▼───┐   ┌───▼──────────────┐
              │  实例列表     │   │  Spec 编辑器     │
              │  [inst #123]  │   │  YAML │ 预览     │
              │  [inst #124]  │   │       │ [Run ▶]  │
              └───────┬───────┘   └───┬──────────────┘
                      │               │
                      └───────┬───────┘
                              ▼
                    ┌─────────────────────┐
                    │  执行查看器(核心)  │
                    │  拓扑画布 + 侧栏    │
                    │  控制 + 事件流      │
                    └─────────────────────┘
```

### 3.3 与现有界面的关系

- **Sidebar "Graphs" 按钮**保留(已有,Workflow 图标)
- **Hash 路由**保留(`useHashRoute.ts` 已支持 4 种 graph 路由: spec 列表/编辑、实例列表/详情)
- **Session 跳转**保留(节点 → `/sessions/{node_id}.{node_name}`)
- 新增:执行查看器内嵌 deliver 操作(当前 `deliverToNode` API 无 UI)

---

## 4. 签名元素 — Deliver 脉冲

### 4.1 概念

当图调度中一个节点向下游节点 deliver 数据时,在拓扑图的对应边上**渲染一个发光的 teal 圆点沿边路径从 source 移动到 target**,留下渐隐尾迹。

这是整个可视化最独特的视觉时刻 — 它让"调度"从"状态快照"变成"可见的流动"。

### 4.2 为什么这是正确的签名

1. **忠于主体**: deliver 是 `modex_graph` 的核心调度机制(节点执行完 → `deliver()` → `route_deliver` → 下游节点被触发)。脉冲不是装饰,是**调度机制的直接视觉投影**。
2. **不可替代的信息**: 静态着色能告诉你"节点 A 已完成,节点 B 在运行",但看不到"数据从 A 流向 B"。脉冲编码了**方向和时序**,这是列表视图完全丢失的。
3. **区别于通用工具**: 大多数 graph 可视化(reactflow/d3/cytoscape 默认渲染)用静态颜色变化表示状态。沿边移动的脉冲是特定于"数据流调度"这个领域的视觉语言,不是模板默认。
4. **克制使用**: 脉冲只在 deliver 事件发生时出现(~600ms),不是常驻动画。无 deliver 时图是静态的。

### 4.3 视觉规格

```
        ┌─────────┐                    ┌─────────┐
        │  Node A │ ●─────────────────▶│  Node B │
        │ done ✓  │  ← teal dot        │  idle   │
        └─────────┘     travels         └─────────┘
                        along edge
                        600ms, ease-out
                        dot: brand-bright, r=4
                        trail: brand 40% → 0%, 24px
```

- **圆点**: `--color-brand-bright` (#14B8A6 light / #5EEAD4 dark), `r=4px`, 带品牌色 12% glow (`filter: drop-shadow`)
- **尾迹**: 跟随圆点的 24px 渐变路径, brand 40% alpha → 0%
- **时长**: 600ms, `--ease-out`
- **触发**: Phase 1 — 客户端 diff 检测节点状态 `pending → running`(推断上游 deliver); Phase 2 — WebSocket `deliver_dispatched` 事件(精确触发)
- **并发**: 多条边同时 deliver 时,多个脉冲并行,不阻塞
- **降级** (`prefers-reduced-motion`): 不显示移动脉冲,改为边短暂高亮(brand 40% → border-strong, 220ms) + target 节点状态徽章更新

### 4.4 活跃节点描边

当节点处于 `running` 状态时,节点外圈有 1.2s 周期的脉动 teal 描边(opacity 0.3 → 0.6 → 0.3),复用 chat 打字指示器的脉冲节奏。

- 形状: **圆角矩形外描边,不是圆环** — 节点是 w=140×h=44 的矩形,圆形环会与矩形边相交、视觉上穿过节点体。取节点 rect 外扩 4px、radius 取 `--radius-md` + 4px 的同形 rect,只画 stroke
- 描边: brand 30% alpha, `stroke-width: 2`
- 周期: 1.2s, `ease-in-out`
- 降级: 静态 brand 40% alpha 描边(不脉动)

---

## 5. 拓扑可视化语言

### 5.1 渲染技术选型

| 方案 | 评估 | 决策 |
|------|------|------|
| reactflow (@xyflow/react) | 功能丰富,但外观"通用 node editor",重(~200kb),默认风格难以摆脱 | ❌ 不用 |
| mermaid (已有依赖) | 静态 SVG 生成,无交互,无动画,布局不可控 | ❌ 不用(已有于 chat,但不适合交互式可视化) |
| d3-force | 物理仿真布局,不适合 DAG(有向图应用结构化布局) | ❌ 不用 |
| **dagre + 自定义 SVG** | dagre 做 DAG 自动布局(TB 方向),自定义 SVG 渲染完全控制视觉语言 | ✅ 采用 |
| 纯手写布局 | 不 scale,每个图都要手动定位 | ❌ 不用 |

**dagre** (`@dagrejs/dagre`, ~30kb) 负责:节点坐标计算、边路径路由(正交/曲线)。**自定义 React SVG 组件**负责:节点视觉、边样式、状态着色、动画。两者分离 — 布局是数据,渲染是设计。

### 5.2 节点视觉规范

#### 形状体系

```
    ┌──────────────┐
    │ ◉  designer   │   ← 功能节点: 圆角矩形
    │    agent      │      w=140, h=44, radius-md
    └──────────────┘

    ●               ← START: 实心圆, r=10, brand fill
    
    ▎               ← END: 实心竖条, w=4 h=20, brand fill
```

所有功能节点(agent/function/delay/human_input/graph)使用**同一种形状**(圆角矩形),通过**类型标签**区分,不通过形状区分。这保持视觉统一,避免"每种类型一个形状"的杂乱。

START/END 是唯一特殊形状 — 它们是结构性的,不是功能性的。

#### 节点解剖

```
┌─ w=140 ──────────────────────────────────┐
│                                           │ h=44
│  ┌──┐  designer               ● running  │
│  │◉ │  ────────               (status)   │
│  │  │  type: agent                        │
│  └──┘  pool: review                       │
│   glyph  name(mono)    status-dot         │
│  (w=20) (flex-1)       (w=8)              │
└───────────────────────────────────────────┘
```

| 区域 | 内容 | 字体/样式 |
|------|------|-----------|
| 左 glyph (20px) | 类型标识 | JetBrains Mono, text-xs, mute color |
| 中 name | 节点名 | Inter 500, text-sm, ink |
| 中 sub | 类型 + pool(如果 agent) | JetBrains Mono, text-xs, faint |
| 右 status dot | 状态颜色点 | 8px 圆, status-colored |

节点名超过内容区宽度时截断加省略号(`text-overflow: ellipsis`),完整名在 tooltip(`title`)与侧栏详情中显示。

#### 类型 glyph

| node_type | glyph | 说明 |
|-----------|-------|------|
| agent | `◉` | 圆环 — 代表"智能体" |
| function | `ƒ` | 函数符号 |
| delay | `◷` | 时钟 |
| human_input | `⏸` | 暂停 — HumanInputNode 挂起等待人工输入 |
| graph | `⬕` | 嵌套图 |
| START | (圆点本身) | 无 glyph |
| END | (竖条本身) | 无 glyph |

glyph 使用 JetBrains Mono, `text-xs`, `text-mute`。不使用颜色区分类型 — 颜色保留给状态。**所有 glyph 渲染时附加 variation selector U+FE0E 强制文本呈现** — `◷`/`⏸` 等符号在部分平台默认渲染为彩色 emoji,会破坏 console 的单色纪律(`✋` 因默认即 emoji 呈现已弃用)。

#### 状态着色

| status | dot color | 边框 | 节点底色 | 说明 |
|--------|-----------|------|---------|------|
| pending | `--color-faint` | `--color-hairline` | `--color-graph-node-fill` | 灰 — 等待中 |
| running | `--color-brand`(空心) | `--color-brand` | `--color-graph-node-fill` | teal — 执行中 + 脉动外描边 |
| completed | `--color-success` =brand(实心) | `--color-hairline` | `--color-graph-node-fill-done` | teal 实心 dot + brand-soft 底色 — 完成 |
| crashed | `--color-danger` | `--color-danger` | `--color-graph-node-fill` | 红 — 崩溃 |
| canceled | `--color-mute` | `--color-hairline` | `--color-graph-node-fill` | 灰 — 取消 |
| suspended | `--color-warning` | `--color-warning` dashed | `--color-graph-node-fill` | 琥珀虚线 — 暂停等待 HumanInput(Phase 2,REST 当前不暴露 suspended 标志) |

**completed vs running 区分**(同色,因 `--color-success` 别名 brand): 单一通道(8px dot 空心 vs 实心)在 reduced-motion 下(活跃描边退化为静态环)几乎不可分辨,因此用**双通道**编码:
- running = 空心 dot + 脉动外描边(降级: 静态 40% 描边)
- completed = 实心 dot + 节点底色 `--color-brand-soft`(既有 token, brand 10~14% tint)

底色差异在任何动效降级下都成立,且不引入新颜色。

### 5.3 边视觉规范

```
Node A ──────────────────────────────▶ Node B
        border-strong stroke, 1.5px
        箭头: 与边同色, 6px
```

| 属性 | 值 | 说明 |
|------|------|------|
| stroke | `--color-border-strong` | 默认中性;hairline 在暗色仅 8% white,边是主要结构信息,需要更高可读性(16%) |
| stroke-width | 1.5px | 细线,不喧宾夺主 |
| 路径 | dagre 直线/正交 | TB 布局自然方向 |
| 箭头 | `<marker>`, 与边同色, 6px | 方向指示;**不用 brand** — 常亮 teal 箭头会稀释"teal = 活跃/流动"的信号 |
| 高亮(deliver 时) | stroke + 箭头 → brand 60%, `--dur` | 脉冲经过时边短暂高亮 |
| 回环边(reviewer→implementer loop) | dagre 曲线路径 | 自然处理,不需特殊样式 |

**无条件边**: GraphSpec edges 是纯拓扑(source→target),无 condition/reason 字段。路由在运行时通过 `deliver()` 决定。因此边**不携带条件标签**,保持简洁。

### 5.4 布局规范

- **方向**: TB (top-to-bottom) — 从 START 到 END 的自然流向
- **节点间距**: dagre `nodesep: 40px` (水平), `ranksep: 60px` (垂直)
- **画布**: SVG `viewBox` 自适应, `min-h: 400px`, 可滚动/缩放
- **缩放**: 鼠标滚轮缩放(0.5x–2x), 拖拽平移 — 复用 SVG `transform`
- **自适应**: 小屏(≤768px)切换为简化列表+拓扑缩略图(不全画布)

### 5.5 MiniTopology(缩略图)

用于 spec 列表和实例列表中的**小型拓扑预览**。

```
  ▦──◇──◇──◇──▦     ← 80×24px SVG, 无文字, 纯结构
   start  nodes  end    node = 6px 圆/方, edge = 1px hairline
```

- 尺寸: 80×24px (固定, 不缩放)
- 节点: 6px 圆(功能节点)/方(START/END)
- 边: 1px hairline
- 状态着色(实例列表用): 每个节点按 status 着色
- 无文字, 无交互 — 纯视觉签名
- dagre 布局, 简化为极简坐标
- **大图省略**: 节点数 > 8 时等比压缩会让节点挤成一团不可读;改为保留 START/END 与首末功能节点,中间链折叠为 `···` 刻度(三个 1px 点,1px hairline 串联)— 结构可读性优先于完整性

---

## 6. 视图逐一设计

### 6.1 执行查看器(GraphExecutionViewer)— 核心视图

这是整个重构的**hero view**。从"扁平节点列表 + 事件列表"重构为"全画布拓扑 + 上下文侧栏"。

#### 布局

```
┌──────────────────────────────────────────────┬──────────────────┐
│ ← Back  #12345 [◉ running]  [⏸][⏹][↗ deliver]│  侧栏(上下文)    │
├──────────────────────────────────────────────┤  w=320, fixed    │
│                                              │                  │
│         ┌─────┐                              │  [选中节点详情]   │
│         │START│ (teal dot)                   │  或 [实例摘要]    │
│           │                                  │  或 [事件时间线]  │
│           ▼                                  │                  │
│      ┌──────────┐  ◉ pulsing ring            │  ──────────────  │
│      │ designer │  (running)                 │  designer        │
│      │  agent   │                            │  type: agent     │
│      └──────────┘                            │  pool: review    │
│           │                                  │  status: running │
│      ●━━━━━━━━━━▶ (deliver pulse)            │  inv: v2         │
│           │                                  │  [→ Open session]│
│           ▼                                  │                  │
│      ┌───────────┐                           │  ──────────────  │
│      │implementer│                           │  Events          │
│      │  agent    │                           │  ● graph_started │
│      └───────────┘                           │  ● node_started  │
│           │     ↺                           │  ● node_done     │
│           ▼                                 │                  │
│      ┌──────────┐                           │                  │
│      │reviewer  │ ✓ (completed, filled)     │                  │
│      │  agent   │                           │                  │
│      └──────────┘                           │                  │
│           │                                  │                  │
│           ▼                                  │                  │
│         ┌─────┐                              │                  │
│         │ END │ (teal bar)                   │                  │
│         └─────┘                              │                  │
│                                              │                  │
├──────────────────────────────────────────────┤                  │
│ 2/4 nodes · 12s elapsed · parallel · on_recv │                  │
└──────────────────────────────────────────────┴──────────────────┘
│ ← 拓扑画布(flex-1, SVG, 可缩放/拖拽)          │ ← 上下文侧栏     │
│ ← 底部摘要条(进度/耗时/调度器/触发模式)       │                  │
```

#### 区域分解

**A. 顶部控制条**(替换当前的 header)
- Back 按钮(ghost)
- Instance ID (mono, text-base)
- 实例状态徽章(GraphStatusBadge, 复用)
- 控制按钮组: Pause / Resume / Stop / **Deliver**(新增)
- 按钮可用性状态机(已有逻辑,保留)

**B. 拓扑画布**(核心新增)
- 全屏 SVG, dagre 布局
- 节点: §5.2 规范
- 边: §5.3 规范
- Deliver 脉冲: §4.3 规范
- 活跃节点描边: §4.4 规范
- 鼠标: 滚轮缩放, 拖拽平移
- 点击节点: 选中 → 侧栏显示节点详情
- 双击 agent 节点: 跳转 session(`onJumpToSession`)
- 画布右上角叠加一行状态图例(mono text-xs faint: `● completed · ◎ running · ● crashed · ○ pending`),帮助用户学习双通道编码;图例是编码系统的说明书,不是装饰
- 键盘可达: 节点 `tabindex="0"` + `role="button"`,Tab 聚焦显示 brand focus ring,Enter 选中 — 符合设计系统 accessibility floor

**C. 底部摘要条**(新增)
- 进度: `X/N nodes completed` (mono)
- 耗时: 从首次 running 开始计时
- 调度器: `linear` / `parallel` (mono, from spec)
- 触发模式: `on_receive` / `on_all_preds` (mono, from spec)
- 这些信息从 spec YAML + instance 状态派生

**D. 上下文侧栏**(替换当前的 Nodes 列表 + Events 列表)
- **选中节点时**: 节点详情面板
  - 节点名 (Inter 500, text-base)
  - 类型 + pool (mono, text-xs, faint)
  - 状态徽章
  - invocation_id / version (mono, text-xs)
  - "Open session" 按钮(agent 节点, ghost, ExternalLink 图标)
  - 节点结果(如果 completed 且 result 可用 — Phase 2 后端暴露)
- **未选中时**: 实例摘要
  - Spec 名 + version
  - 调度器 + 触发模式
  - 进度环(X/N, circular progress)
  - 耗时
  - **图级结果**(实例 completed 时): 来自 `GET /instances/{id}` 的 `result` 字段(END 聚合),**Phase 1 即可展示** — 无需等 Phase 2;节点级结果才是 Phase 2(§11.4)
- **底部: 事件时间线**(始终可见)
  - 竖直时间线, 最新在底
  - 每条: 状态色圆点 + kind (mono) + 可展开 result/error
  - Phase 1: 事件由状态 diff 派生(见 §9.3),客户端时间戳,标注为推断
  - Phase 2: 真实 WS 事件,带后端 timestamp

#### Deliver 交互(新增)

当前 `deliverToNode` API 已实现但无 UI。新增 deliver 流程:

1. 控制条 "Deliver" 按钮(running/paused 时可用)
2. 点击 → 弹出 DeliverDialog(modal)
3. Dialog 内容:
   - 节点选择器(SelectMenu, 列出 instance.nodes)
   - 内容输入(Textarea, "Deliver content...")
   - 确认/取消
4. 确认 → `deliverToNode(ws, id, nodeName, content)` → 刷新
5. 成功 toast: `Delivered to {node_name}`

#### 数据流(Phase 1 — 纯前端)

```
GET /api/graphs/specs/{spec_id}          ← 一次性, 取 YAML
    │
    ▼
parseGraphSpecYaml(yaml)                 ← 前端 YAML 解析
    │
    ▼
{ nodes: NodeSpec[], edges: EdgeSpec[], scheduler, ... }
    │
    ▼
dagre.layout(graph)                      ← 布局计算
    │
    ▼
{ nodePositions, edgePaths }             ← 渲染数据
    │
    ├──▶ <TopologyCanvas> SVG 渲染       ← 节点 + 边(静态结构)
    │
GET /api/graphs/instances/{id}           ← 轮询 2s
    │
    ▼
{ status, nodes: [{node_name, node_id, status}], result }
    │
    ▼
diffNodeStatuses(prev, current)          ← 客户端 diff
    │
    ├── 状态变更 → 节点着色更新
    ├── pending→running → 触发上游边 deliver 脉冲
    └── running→completed → 触发下游边 deliver 脉冲

GET /api/graphs/instances/{id}/events    ← 轮询 2s (best-effort)
    │
    ▼
事件时间线更新
```

#### 数据流(Phase 2 — WebSocket 增强)

```
WebSocket (/ws)                          ← 复用现有 WS 基础设施
    │
    ▼
graph_event 消息类型(新增)
    │
    ├── { kind: "node_started", node_id, invocation_id, timestamp }
    ├── { kind: "node_completed", node_id, invocation_id, timestamp }
    ├── { kind: "deliver_dispatched", source_node_id, target_node_id, timestamp }
    └── { kind: "graph_completed" | "graph_crashed", ... }
    │
    ▼
精确触发 deliver 脉冲(source→target 对应真实边)
精确触发节点状态变更(无需 diff)
精确事件时间线(带 timestamp)
```

### 6.2 Spec 编辑器(GraphSpecEditor)— 分栏视图

从纯 Textarea 重构为 **YAML 编辑器 + 实时拓扑预览** 分栏。

#### 布局

```
┌───────────────────────┬───────────────────────┐
│ ← Back  review_wf     │                       │
├───────────────────────┤  Topology Preview     │
│ CodeMirror YAML       │  (live SVG)           │
│                       │                       │
│ 1  name: review_wf    │     [START]           │
│ 2  version: "1.0"     │       │               │
│ 3  scheduler: parallel│       ▼               │
│ 4  nodes:             │   [designer]          │
│ 5    - name: designer │       │               │
│ 6      node_type:agent│       ▼               │
│ 7      config:        │   [implementer]       │
│ 8        agent: design│       │  ↺            │
│ 9        pool: review │       ▼               │
│ 10   - name: implement│   [reviewer]          │
│ ...                   │       │               │
│                       │       ▼               │
│ ┌───────────────────┐ │     [END]             │
│ │ ✗ line 12: unknown│ │                       │
│ │   node_type 'foo' │ │  ────────────────    │
│ └───────────────────┘ │  User input:          │
│                       │  [_______________]    │
│ [Save]     [Run ▶]    │                       │
└───────────────────────┴───────────────────────┘
│ ← YAML 编辑器(50%)     │ ← 预览+运行(50%)     │
```

#### 区域分解

**A. YAML 编辑器(左栏)**
- **CodeMirror 6** 替换 Textarea
  - `@codemirror/lang-yaml`: YAML 语法高亮
  - `@codemirror/view`: line numbers, 活跃行高亮
  - 主题: 自定义 Teal & Ember CodeMirror theme(背景 = canvas-elevated, 文字 = ink, 注释 = mute, 关键字 = brand)
  - 行号: mono, text-xs, faint
- **内联校验**: 编辑时不做实时校验(避免频繁请求);Save 时后端校验,错误以 CodeMirror lint marker 标注对应行 + 下方错误面板
- 保存: `PUT /specs/{id}` → 成功更新预览 + "Saved" 指示;失败显示行级错误

**B. 拓扑预览(右栏上半)**
- 输入: 编辑器当前 YAML 内容(防抖 300ms)
- 解析: `parseGraphSpecYaml(yaml)` → 如果解析失败,预览保持上次有效状态 + 显示解析错误
- 渲染: 与执行查看器相同的 SVG 拓扑组件(无状态着色,纯结构)
- 布局: dagre TB
- 交互: 可缩放/拖拽;点击节点显示节点配置 tooltip
- **回环边可视化**: reviewer→implementer loop 正常渲染(dagre 处理)

**C. 运行区(右栏下半)**
- User input 输入(Input, optional)
- Run 按钮(primary, Play 图标) → `POST /specs/{id}/run` → navigate to 执行查看器
- 保存状态指示("Saved" / "Unsaved changes")

**D. 错误面板(左栏底部, 条件显示)**
- 校验错误: 行号 + 错误信息(danger 色, mono)
- CodeMirror 对应行有 lint gutter marker

#### 小屏适配

≤768px: 分栏切换为 tab( YAML / Preview 两个 tab), 保留 Run 按钮在顶部。

### 6.3 Spec 列表(GraphConfigPage → GraphSpecListPage)

保留当前结构,每行增加 **MiniTopology 缩略图** + 元信息。

#### 布局

```
┌──────────────────────────────────────────────────────────┐
│  GRAPHS                                  [Instances →]   │
├──────────────────────────────────────────────────────────┤
│                                                          │
│  ▦──◇──◇──◇──▦  review_workflow        v1.0             │
│                  3 nodes · parallel · on_receive   [Edit]│
│                                                          │
│  ▦──◇──▦        simple                  v1.0             │
│                  0 nodes · linear · on_all_preds    [Edit]│
│                                                          │
│  ▦──◇──◇──▦     coder_pipeline          v2.1             │
│                  2 nodes · linear · on_all_preds    [Edit]│
│                                                          │
└──────────────────────────────────────────────────────────┘
```

- 每行: MiniTopology (80×24px) + spec name (Inter 500) + version (mono, faint) + 节点数/调度器/触发模式 (mono, text-xs, mute) + Edit 按钮(ghost)
- MiniTopology 从 spec YAML 解析得到(列表加载时批量解析)
- 空状态: "No graph specs. Add a YAML file to config/graphs/."
- 保持 max-w-3xl 居中

### 6.4 实例列表(GraphInstanceListPage)

每行增加 **MiniTopology(状态着色)** + 进度。

#### 布局

```
┌──────────────────────────────────────────────────────────┐
│  ← Back                                [status: all  ▾]  │
├──────────────────────────────────────────────────────────┤
│  INSTANCES                                               │
├──────────────────────────────────────────────────────────┤
│  ▦──◉──◇──◇──▦  #12345  review_workflow                 │
│                  [running]   2/4 nodes · 12s       [→]   │
│                                                          │
│  ▦──◉──◉──◉──▦  #12344  review_workflow                 │
│                  [completed] 4/4 nodes · 45s       [→]   │
│                                                          │
│  ▦──◉──✕──◇──▦  #12343  review_workflow                 │
│                  [crashed]   2/4 nodes · 8s        [→]   │
│                                                          │
│  ▦──◐──◇──◇──▦  #12342  simple                           │
│                  [paused]    1/2 nodes · 5s        [→]   │
│                                                          │
└──────────────────────────────────────────────────────────┘
```

- 每行: MiniTopology(节点按 status 着色: ◉=running, ✓=completed, ✕=crashed, ◐=paused, ◇=pending) + instance ID (mono) + spec name + 状态徽章 + 进度 + 耗时 + 箭头
- 状态过滤 SelectMenu 保留
- MiniTopology 需要 spec YAML(从 instance 的 spec_id 关联获取) — 列表加载时并行获取相关 spec

---

## 7. 令牌扩展(Graph 语义别名)

所有新增 token **映射到已有 color/radius/motion token**,不引入新色值。

### 7.1 Graph 语义 token(添加到 `index.css` `:root` / `.dark`)

```css
/* ── Graph visualization semantic tokens ──────────────────── */
/* All derive from existing tokens — no new colors introduced. */

/* Node fill/border — use existing canvas/hairline */
--color-graph-node-fill: var(--color-canvas-elevated);
--color-graph-node-fill-done: var(--color-brand-soft);  /* completed 双通道编码: 底色 tint */
--color-graph-node-border: var(--color-hairline);
--color-graph-node-border-active: var(--color-brand);

/* Edge — border-strong stroke + neutral arrowhead; teal reserved for active/highlight */
--color-graph-edge: var(--color-border-strong);
--color-graph-edge-active: color-mix(in srgb, var(--color-brand) 60%, transparent);
--color-graph-arrow: var(--color-border-strong);
--color-graph-arrow-active: color-mix(in srgb, var(--color-brand) 60%, transparent);

/* Deliver pulse — brand-bright dot + brand trail */
--color-graph-deliver: var(--color-brand-bright);
--color-graph-deliver-glow: color-mix(in srgb, var(--color-brand-bright) 12%, transparent);
--color-graph-deliver-trail: color-mix(in srgb, var(--color-brand) 40%, transparent);

/* Active node ring — brand pulse */
--color-graph-active-ring: color-mix(in srgb, var(--color-brand) 30%, transparent);

/* MiniTopology */
--color-graph-mini-node: var(--color-faint);
--color-graph-mini-edge: var(--color-hairline);
--color-graph-mini-start: var(--color-brand);
--color-graph-mini-end: var(--color-brand);
```

### 7.2 Motion 扩展

```css
/* Deliver pulse — longer than standard motion, information-bearing */
--dur-deliver: 600ms;
--ease-deliver: var(--ease-out);

/* Active node ring pulse — matches typing indicator rhythm */
--dur-ring-pulse: 1200ms;
--ease-ring-pulse: ease-in-out;

/* Graph layout transition (re-layout on spec change) */
--dur-layout: 350ms;  /* = --dur-slow */
```

### 7.3 Tailwind 映射

`tailwind.config.js` 增加 graph token → utility 映射(与现有 token 映射方式一致):

```js
colors: {
  // ... existing ...
  'graph-node': 'var(--color-graph-node-fill)',
  'graph-edge': 'var(--color-graph-edge)',
  'graph-deliver': 'var(--color-graph-deliver)',
  // ...
}
```

---

## 8. 动效规范

### 8.1 动效清单

| 动效 | 触发 | 时长 | 缓动 | 属性 | 降级 |
|------|------|------|------|------|------|
| Deliver 脉冲 | deliver 事件(WS) / 状态 diff(`*→completed`,见 §9.3) | 600ms | `--ease-out` | cx/cy 沿 path + opacity | 边高亮 220ms |
| 活跃节点描边 | node.status === running | 1200ms 循环 | ease-in-out | stroke-opacity 0.3→0.6→0.3 | 静态 40% 描边 |
| 节点状态变色 | status 变更 | `--dur` (220ms) | `--ease-out` | fill/stroke color | 即时变更 |
| 节点崩溃闪烁 | `*→crashed` | `--dur` (220ms) | `--ease-out` | border/fill → danger | 静态 danger 边框 |
| 布局重排 | spec 编辑导致拓扑变化 | `--dur-slow` (350ms) | `--ease-out` | transform translate | 即时重排 |
| 选中节点高亮 | click node | `--dur-fast` (150ms) | `--ease-out` | border-color + shadow | 即时 |

注: motion token 体系中缓动只有 `--ease-out` 一个(`--dur-fast/--dur/--dur-slow` 是时长)。MiniTopology 入场 stagger 已按 §15.4 取下,不在清单中。

### 8.2 降级规则

`@media (prefers-reduced-motion: reduce)`:
- Deliver 脉冲 → 边 220ms 高亮(brand 40% → border-strong)
- 活跃描边 → 静态 brand 40% 描边(不循环)
- 崩溃闪烁 → 静态 danger 边框
- 状态变色 → 即时(0ms)
- 布局重排 → 即时

**信息不丢失**: 降级后用户仍能看到状态着色、选中高亮、边方向 — 只是失去运动感。

### 8.3 不做的动效

- ❌ 节点持续浮动/呼吸(纯装饰)
- ❌ 边持续流光(非 deliver 时的装饰)
- ❌ 页面切换粒子效果(graph 视图不需要)
- ❌ 3D/parallax(与 console 定位不符)

---

## 9. 组件架构

### 9.1 新增组件树

```
components/graphs/
├── GraphSpecListPage.tsx          (重构: + MiniTopology)
├── GraphSpecEditor.tsx            (重构: 分栏 + CodeMirror)
├── GraphInstanceListPage.tsx      (重构: + MiniTopology + 进度)
├── GraphExecutionViewer.tsx       (重构: 全画布 + 侧栏)
├── DeliverDialog.tsx              (新增: deliver 操作 modal)
├── shared.tsx                     (保留: GraphStatusBadge + formatGraphApiError)
├── topology/                      (新增: 拓扑可视化组件族)
│   ├── TopologyCanvas.tsx         (SVG 画布: 缩放/平移/渲染容器)
│   ├── GraphNode.tsx              (单个节点 SVG: 形状/glyph/状态)
│   ├── GraphEdge.tsx              (单条边 SVG: 路径/箭头/高亮)
│   ├── DeliverPulse.tsx           (deliver 脉冲动画 SVG)
│   ├── ActiveNodeRing.tsx         (活跃节点脉动环 SVG)
│   ├── MiniTopology.tsx           (缩略图: 80×24px 简化拓扑)
│   └── layout.ts                  (dagre 布局封装: spec → 坐标)
├── detail/
│   ├── NodeDetailPanel.tsx        (侧栏: 选中节点详情)
│   ├── InstanceSummary.tsx        (侧栏: 实例摘要 + 进度环)
│   └── EventTimeline.tsx          (侧栏: 事件时间线)
└── yaml/
    ├── YamlCodeEditor.tsx         (CodeMirror 封装: YAML + lint)
    └── parseGraphSpec.ts          (YAML → 结构化 topology model)
```

### 9.2 核心数据模型(前端)

```typescript
// parseGraphSpec.ts — YAML → 结构化拓扑

interface ParsedGraphTopology {
  name: string;
  scheduler: "linear" | "parallel";
  defaultTrigger: "on_receive" | "on_all_preds";
  nodes: ParsedNode[];
  edges: ParsedEdge[];
  entryNode: string;  // always "__start__"
}

interface ParsedNode {
  name: string;
  nodeType: "agent" | "function" | "delay" | "human_input" | "graph" | "__start__" | "__end__";
  config: { agent?: string; pool?: string };
  trigger?: string;
}

interface ParsedEdge {
  source: string;
  target: string;
}

// layout.ts — dagre 布局结果

interface LayoutResult {
  nodes: Map<string, { x: number; y: number; width: number; height: number }>;
  edges: Map<string, { points: { x: number; y: number }[] }>;  // edge key = `${source}-${target}`
}
```

### 9.3 状态 diff(Phase 1 实时性)

```typescript
// useGraphExecution.ts (新 hook)

function diffNodeStatuses(
  prev: GraphNodeStatus[],
  current: GraphNodeStatus[],
): NodeStatusTransition[] {
  // 对每个节点,比较 prev.status → current.status
  // 返回状态变更列表:
  //   { nodeId, nodeName, from: "pending", to: "running", timestamp: Date.now() }
}
```

**脉冲触发只认一个来源 — 节点完成**: `*→completed` 时在该节点的所有出边上触发 deliver 脉冲。**不在 `pending→running` 时触发入边脉冲** — 否则同一条边会被"上游完成"与"下游启动"连续触发两次(2s 轮询下两个 transition 常被同一次 poll 观测到,脉冲翻倍,流动的节奏感反而被破坏)。

转换处理表:

| 观测到的 transition | 含义(2s 轮询下) | 动作 |
|--------------------|----------------|------|
| `pending→running` | 节点启动 | 仅更新状态着色 + 活跃描边 |
| `*→completed` | 节点完成,已 deliver 下游 | 出边 deliver 脉冲 + 状态着色 |
| `pending→completed`(跳变) | 节点在轮询间隔内完成整个生命周期 | 出边脉冲(同 `*→completed`) |
| `*→crashed` | 节点崩溃 | 节点闪烁 danger 220ms + 状态着色 |
| 其他 | — | 状态着色 |

**派生事件时间线**: Phase 1 的 `/events` 只有终态事件(`graph_completed`/`graph_crashed`),侧栏时间线几乎为空。diff 出的每个 transition 同时**派生一条本地时间线事件**(`node_started`/`node_completed`/`node_crashed`,客户端时间戳,UI 标注为推断);Phase 2 切换到 WS 真实事件后同槽位替换,时间线 UI 不变。

---

## 10. 依赖新增

| 依赖 | 用途 | 大小 | 必要性 |
|------|------|------|--------|
| `@dagrejs/dagre` | DAG 自动布局 | ~30kb | 必须 — 不手写布局算法 |
| `@codemirror/state` + `@codemirror/view` + `@codemirror/lang-yaml` + `@codemirror/lint` | YAML 编辑器 | ~100kb (tree-shakeable) | 必须 — 替换 Textarea |
| `yaml` | 前端 YAML 解析(v2, ESM-friendly;不是 js-yaml) | ~40kb | 必须 — parseGraphSpec 需要 |

**总新增**: ~170kb (tree-shaken)。可接受 — graph 可视化是重功能。

**不引入**: reactflow(~200kb, 外观通用), d3全量(只需 dagre), cytoscape(过重)。

**已有可复用**: `mermaid`(已存在,用于 chat,不用于 graph 可视化), `lucide-react`(图标), `react-markdown`(如果节点详情需要渲染 markdown 结果)。

---

## 11. 后端改动需求(Phase 2)

Phase 1 纯前端可交付完整可视化体验(拓扑 + diff-based 脉冲 + 控制面板)。Phase 2 提升实时性和数据精度。

### 11.1 节点级事件(必须)

**当前**: `GraphOutputKind` 只有 `graph_completed` / `graph_crashed`,只在图终止时发射一次。

**需要**: 扩展节点级事件:

```python
# src/modex_graph/output_adapter.py
class GraphOutputKind(StrEnum):
    graph_completed = "graph_completed"
    graph_crashed = "graph_crashed"
    # 新增:
    node_started = "node_started"        # Node.run begin_invocation 时
    node_completed = "node_completed"    # Node.run complete_invocation 时
    node_crashed = "node_crashed"        # Node.run except 时
    deliver_dispatched = "deliver_dispatched"  # coordinator.route_deliver 时
```

**发射点**:
- `node_started`: `Node.run()` 的 `begin_invocation` 之后
- `node_completed`: `Node.run()` 的 `complete_invocation` 之后
- `node_crashed`: `Node.run()` 的 except 分支
- `deliver_dispatched`: `GraphPersistenceCoordinator.route_deliver()` 中

**GraphOutput 扩展**:
```python
class GraphOutput(BaseModel):
    kind: GraphOutputKind
    graph_instance_id: int
    result: Any = None
    error: str | None = None
    # 新增:
    node_id: str | None = None           # 节点级事件携带
    node_name: str | None = None
    invocation_id: int | None = None
    target_node_id: str | None = None    # deliver_dispatched 携带
    timestamp: int | None = None         # epoch ms, 所有事件
```

### 11.2 WebSocket 图事件通道(推荐)

**当前**: graph 事件纯 REST 轮询(2s)。现有 `/ws` 是 action 分发协议(`attach`/`send_message`/`pause`/`delete_conversation`,见 `routes/websocket/__init__.py` 的 `dispatch_ws_message`),出站流是 per-session delta queue + `forward_deltas` 转发循环 + `watch_new_queues` 认领动态队列。

**方案(已对照现有 WS 基础设施对齐)**: 不复用 session delta queue(那是会话流式通道,按 conversation 前缀认领),而是**按同一套模式新增 instance 作用域的事件通道**:

- **新 action**: `WebSocketAction` 新增 `SUBSCRIBE_GRAPH = "subscribe_graph"` / `UNSUBSCRIBE_GRAPH = "unsubscribe_graph"`;`dispatch_ws_message` 增加对应分支;handler 放新子模块 `routes/websocket/graph.py`(遵循一 action 一子模块的现有约定)。订阅消息携带 `instance_id` + `ws`(workspace 标识,与其他 action 一致),经 `_resolve_ws_request` 解析到 workspace 的 graph 资源。
- **连接状态**: `_WsConnectionState` 增加 `subscribed_graphs: list[int]` + 对应 forward task;`cleanup()` 统一注销(与 attached_sessions 同一生命周期)。重新 attach 会话**不**清 graph 订阅(两者正交)。
- **生产者收敛点**: `WebUIGraphOutputAdapter.emit()` 是唯一发射缝 — 在现有"写内存 `graph_event_store`"之外,同时 fan-out 到该 instance 的订阅队列(`graph_event_subscribers: dict[int, list[asyncio.Queue]]`,与 event_store 一样挂在 workspace 资源上)。REST 轮询路径不受影响,双通道共用同一 emit。
- **转发循环**: 订阅时为每个 (connection, instance) 启动一个类似 `forward_deltas` 的 drain 循环,队列消费即推 WS;取消订阅/断连时取消 task 并摘除队列。
- **消息形状**: 不走 `DeltaEnvelope`(它要求 session_id,且语义是会话流)。图事件用独立形状:

```json
{ "type": "graph_event", "graph_instance_id": "12345", "event": { "kind": "deliver_dispatched", "node_id": "...", "target_node_id": "...", "timestamp": 1733000000000 } }
```

前端 `ws-client.ts` 的 `isEnvelope` 检查不匹配该形状,会走既有 legacy passthrough;在 App 层按 `type === "graph_event"` 分流到 graph 处理(不进 chat reducer),`useGraphExecution` WS 模式消费。

**优势**: 精确 deliver 脉冲触发(source→target 对应真实边),实时节点状态(无需 diff),带 timestamp 的事件时间线;协议、生命周期、清理全部复用现有 WS 模式,无第二套连接。

### 11.3 拓扑端点(可选优化)

**当前**: 前端需解析 spec YAML 得到拓扑结构。

**可选**: 新增 `GET /api/graphs/specs/{spec_id}/topology`:

```python
class GraphTopologyResponse(BaseModel):
    spec_id: str
    name: str
    scheduler: str
    default_trigger: str
    nodes: list[NodeTopologyInfo]   # name, type, config, trigger
    edges: list[EdgeTopologyInfo]   # source, target
    entry_node: str
```

**好处**: 前端不需要 YAML 解析(减少 ~30kb yaml 依赖 + 解析错误风险);拓扑数据后端权威(compiler 校验后的结构)。

**优先级**: 低 — 前端 YAML 解析可行且 spec 结构简单。如果后端已有 compiler 产出的 CompiledGraph,暴露它成本很低。

### 11.4 节点中间结果暴露(可选)

**当前**: `NodeInvocationRecord.state_json` 存在但 REST 响应未暴露。`GraphInstance.result` 只有图级终态结果。

**可选**: `NodeStatusInfo` 扩展 `result: GraphPayload | None`(completed 节点的 deliver 内容摘要),让侧栏节点详情显示节点输出。

**优先级**: 中 — 对调试图执行很有价值,但需考虑数据量(deliver content 可能很大)。

---

## 12. 分期策略

实现 ticket 拆分见同目录 `tickets.md`(G01~G12,含依赖 frontier 与验收清单);本节是两个 Phase 的目标与范围界定。

### Phase 1 — 纯前端完整可视化(无后端改动)

**目标**: 用现有 REST API 交付完整可视化体验。

| 工作项 | 依赖 | 验证标准 |
|--------|------|----------|
| `parseGraphSpec.ts` — YAML 解析 | yaml 依赖 | 正确解析 simple.yml + review_workflow.yml |
| `layout.ts` — dagre 布局封装 | dagre 依赖 | 节点不重叠,边不交叉,TB 方向 |
| `TopologyCanvas` + `GraphNode` + `GraphEdge` | layout.ts | 渲染 review_workflow 的 5 节点 5 边拓扑 |
| `DeliverPulse` + `ActiveNodeRing` | TopologyCanvas | 脉冲沿边移动,活跃节点环脉动 |
| `GraphExecutionViewer` 重构 | 全部 topology 组件 | 全画布拓扑 + 侧栏 + 控制条 + 摘要条 |
| `useGraphExecution` hook — 轮询 + diff | graphsApi | 2s 轮询,diff 检测状态变更,触发脉冲 |
| `MiniTopology` | layout.ts | 80×24px 缩略图,正确渲染结构 |
| `GraphSpecListPage` 重构 | MiniTopology | 每行有缩略图 + 元信息 |
| `GraphInstanceListPage` 重构 | MiniTopology | 每行有状态着色缩略图 + 进度 |
| `YamlCodeEditor` — CodeMirror 封装 | CodeMirror 依赖 | YAML 高亮 + 行号 + lint marker |
| `GraphSpecEditor` 重构 | YamlCodeEditor + TopologyCanvas | 分栏: YAML ↔ 实时预览 |
| `DeliverDialog` | graphsApi.deliverToNode | modal 选节点 + 输内容 + 提交 |
| `NodeDetailPanel` + `InstanceSummary` + `EventTimeline` | — | 侧栏三模式切换 |
| Token 扩展(`index.css`) | — | graph 语义 token 添加 + tailwind 映射 |
| `prefers-reduced-motion` 降级 | — | 脉冲→边高亮,环→静态,信息不丢 |
| i18n 扩展 | — | 新增 label key(deliver, progress, elapsed 等) |
| 测试 | — | topology 组件单元测试 + diff 逻辑测试 |

**Phase 1 交付后用户可**: 可视化图结构,观察执行流(基于 diff 的脉冲),编辑 YAML 带实时预览,完整控制(暂停/恢复/停止/deliver),查看节点状态和事件。

### Phase 2 — 后端实时事件(增强)

**目标**: WebSocket 精确事件替代 REST 轮询 + diff 推断。

| 工作项 | 归属 | 验证标准 |
|--------|------|----------|
| `GraphOutputKind` 扩展(node_started/completed/crashed, deliver_dispatched) | modex_graph | 事件在 Node.run 生命周期正确发射 |
| `GraphOutput` 扩展(node_id, target_node_id, timestamp) | modex_graph | 事件携带完整上下文 |
| `WebUIGraphOutputAdapter` 双通道(内存 + WS) | bot_project | emit 同时写 event_store + 推 WS |
| WS graph 事件消息类型 + 订阅协议 | bot_project | `subscribe_graph` → 实时推送 |
| `useGraphExecution` WS 模式(替代轮询) | webui | WS 事件驱动脉冲/状态,无 diff |
| 事件时间线带 timestamp | webui | 每条事件有真实时间戳 |
| `GET /specs/{id}/topology` 端点(可选) | bot_project | 返回结构化拓扑 |
| 节点中间结果暴露(可选) | bot_project + modex_agent | NodeStatusInfo 带 result |

**Phase 2 交付后用户可**: 实时看到精确的 deliver 流向(不是推断),毫秒级节点状态更新,带时间戳的事件时间线,节点输出内容查看。

---

## 13. 不做什么(明确 out-of-scope)

| 不做 | 原因 | 后续 |
|------|------|------|
| 可视化拖拽编辑器(模式 B) | ticket 10 明确标注后续增强;YAML 编辑器(模式 A)是本期 scope | 远期:拖拽生成 YAML |
| GraphSpec POST 创建 / DELETE 删除 | ticket 09 明确不做;YAML 文件管理为主 | 远期 REST 端点 |
| GraphSpec 热更新 | 启动时加载,更新需重启或手动 reload | 远期增强 |
| 动态图拓扑 / AdaptiveNode / GraphRAG | PRD 明确远期 | 不在本期 scope |
| Postgres 后端 / 子图独立 checkpoint | PRD future-cap | 远期增强 |
| 图执行回放(时间轴拖拽回到过去某时刻) | 需要完整状态快照历史,当前不存 | 远期增强 |
| 多图实例并排对比 | 当前单实例查看已足够复杂 | 如果有需求再考虑 |
| 节点内 agent 对话实时嵌入(图内 mini-chat) | 跳转 session 已满足;mini-chat 复杂度过高 | 远期增强 |

---

## 14. 风险与缓解

| 风险 | 影响 | 缓解 |
|------|------|------|
| dagre 布局对回环边(reviewer→implementer)处理不佳 | 回环边可能与其他边重叠 | dagre 支持回环;如果重叠,调整 `ranksep` 或手动偏移回环边 |
| Phase 1 diff 推断 deliver 方向不准确 | 脉冲可能在错误的边上触发 | diff 检测 `pending→running` 时,在该节点**所有入边**触发脉冲(保守);`running→completed` 时在**所有出边**触发。Phase 2 WS 修正为精确 source→target |
| CodeMirror 增加包体积(~100kb) | 首屏加载变慢 | CodeMirror 按需加载(lazy import);spec 编辑器不是首屏,不影响 chat 首屏 |
| 大图(20+ 节点)布局拥挤 | 拓扑不可读 | 缩放/平移交互;节点数 >15 时提示"图较大,建议缩放查看";远期考虑折叠子图 |
| YAML 解析在前端失败(spec 结构复杂) | 预览不渲染 | 解析失败时保持上次有效预览 + 显示解析错误;不阻塞编辑 |
| Phase 2 WS 事件量大(高频 node 事件) | 性能压力 | 节点级事件限速(throttle 100ms);客户端批量处理;非订阅 instance 不推送 |

---

## 15. 设计自审(Critique)

### 15.1 对照 AI 默认模板检查

| AI 默认模板 | 本设计是否落入? | 判断 |
|------------|----------------|------|
| 暖白底 + 高对比衬线 + 赤陶强调 | ❌ — 使用既有 Warm Graphite + teal + Inter/JetBrains Mono | 不落入 |
| 近黑底 + 单一亮色(酸绿/朱红) | ❌ — teal 是品牌色,不是"单亮色"方案;有完整的 token 系统 | 不落入 |
| 报纸风 + hairline + 零圆角 + 密集列 | ❌ — 有圆角(radius-md),不是报纸布局 | 不落入 |

### 15.2 签名元素独特性

Deliver 脉冲是否是"任何类似项目都会做的默认选择"?

- reactflow/d3/cytoscape 默认渲染:静态节点 + 静态边 + 颜色变化。**不做沿边移动的脉冲。**
- Langflow / Flowise(agent workflow builders):静态拓扑 + 状态着色。**不做 deliver 脉冲。**
- Temporal/Cadence(workflow engines):事件日志 + 图表。**不做 deliver 脉冲。**

沿边移动的 deliver 脉冲是**特定于"deliver 是图调度的核心机制"这个领域知识的视觉选择**,不是通用 graph 可视化的默认。它来自对 `modex_graph` 的 `deliver()` / `route_deliver()` 机制的理解,将调度机制直接投影为视觉运动。

**结论**: 签名元素足够独特。

### 15.3 与既有设计系统一致性

- ✅ 调色板: 全部从 `--color-*` token 派生,无新色值
- ✅ 字体: Inter + JetBrains Mono,无新字体
- ✅ 圆角: 使用 `--radius-md` / `--radius-sm`
- ✅ 动效: 使用 `--ease-out` / `--dur-*`,新增 `--dur-deliver` 合理扩展
- ✅ 暗色: graph token 自动跟随 `.dark` 块(因为映射到 `--color-*`)
- ✅ 无障碍: `prefers-reduced-motion` 降级,信息不丢失
- ✅ 共享 UI 原语: 复用 Card/Button/SectionLabel/SelectMenu/GraphStatusBadge

### 15.4 Chanel 原则 — 取下一件配饰

审视全设计,最可能"过度"的元素:

1. **节点类型 glyph**(◉/ƒ/◷/✋/⬕)— 是否必要? 是的,它编码节点类型信息,不是装饰。保留。
2. **底部摘要条** — 是否多余? 不多余,它把分散的信息(进度/耗时/调度器)集中,避免侧栏过载。保留。
3. **MiniTopology 入场 stagger 动画** — 这是最接近"装饰"的元素。列表入场 stagger 确实是锦上添花。**取下**: MiniTopology 入场不做 stagger,即时显示。列表行 hover 效果已足够。

**取下后**: 设计更克制,签名元素(deliver 脉冲)更突出。

---

## 16. 文案规范(i18n)

遵循 webui AGENTS.md 的 i18n 约定:所有显示文案通过 `useT()` + `MessageKey`,不硬编码。

### 新增 i18n key(英文基准)

```typescript
// graphs namespace 扩展
graphs: {
  // 现有 keys 保留...
  
  // 新增 — 执行查看器
  progress: "progress",                          // "2/4 nodes"
  elapsed: "elapsed",                            // "12s"
  scheduler: "scheduler",                        // label
  triggerMode: "trigger mode",                   // label
  deliver: "deliver",                            // 按钮
  deliverDialogTitle: "Deliver to node",
  deliverNodeLabel: "Node",
  deliverContentLabel: "Content",
  deliverContentPlaceholder: "Enter content to deliver...",
  deliverConfirm: "Deliver",
  deliverSuccess: "Delivered to {name}",
  
  // 新增 — 侧栏
  instanceSummary: "instance",
  nodeDetail: "node",
  eventType: "event",
  openSession: "Open session",
  invocationId: "invocation",
  version: "version",  // 已有

  // 新增 — 画布状态图例
  legendCompleted: "completed",
  legendRunning: "running",
  legendCrashed: "crashed",
  legendPending: "pending",
  
  // 新增 — Spec 编辑器
  preview: "preview",
  unsavedChanges: "unsaved changes",
  parseError: "YAML parse error",
  
  // 新增 — 空状态
  noGraphsHint: "Add a YAML file to config/graphs/ to create a graph spec.",
}
```

### 文案原则

- 动词主动语态: "Deliver to node" 不是 "Delivery"
- 操作名贯穿流程: 按钮说 "Deliver" → toast 说 "Delivered to {name}"
- 空状态是行动邀请: "Add a YAML file..." 不是 "No specs found"
- 错误明确: "YAML parse error: {detail}" 不是 "Something went wrong"

---

## 附录 A: 现有组件改造对照

| 现有文件 | 行数 | 改造 |
|---------|------|------|
| `GraphConfigPage.tsx` | 95 | 重命名 `GraphSpecListPage`,加 MiniTopology + 元信息 |
| `GraphSpecEditor.tsx` | 153 | 分栏重构: CodeMirror + TopologyCanvas |
| `GraphListPage.tsx` | 115 | 重命名 `GraphInstanceListPage`,加 MiniTopology + 进度 |
| `GraphExecutionViewer.tsx` | 241 | 全面重构: 全画布拓扑 + 侧栏 |
| `shared.tsx` | 54 | 保留(GraphStatusBadge + formatGraphApiError 复用) |
| `graphsApi.ts` | 228 | 保留(12 端点不变),可能加 `getTopology`(Phase 2) |
| `useHashRoute.ts` | 57 | 保留(路由不变) |
| `App.tsx` | — | 路由切换不变,组件 import 名更新 |

## 附录 B: 示例图拓扑数据(review_workflow.yml)

```yaml
# 解析后的结构化拓扑
{
  name: "review_workflow",
  scheduler: "parallel",
  defaultTrigger: "on_receive",
  nodes: [
    { name: "__start__", nodeType: "__start__" },
    { name: "designer", nodeType: "agent", config: { agent: "designer", pool: "review" } },
    { name: "implementer", nodeType: "agent", config: { agent: "implementer", pool: "review" } },
    { name: "reviewer", nodeType: "agent", config: { agent: "reviewer", pool: "review" } },
    { name: "__end__", nodeType: "__end__" },
  ],
  edges: [
    { source: "__start__", target: "designer" },
    { source: "designer", target: "implementer" },
    { source: "implementer", target: "reviewer" },
    { source: "reviewer", target: "implementer" },   // 回环
    { source: "reviewer", target: "__end__" },
  ],
  entryNode: "__start__",
}
```

```
# dagre TB 布局结果(示意)

      [START]
        │
        ▼
    [designer]
        │
        ▼
    [implementer] ◀──┐
        │             │
        ▼             │
    [reviewer] ───────┘  (回环)
        │
        ▼
      [END]
```

---

## 附录 C: Rev 2 评审修订记录

设计评审(对照 `index.css` token 实况、`useHashRoute.ts`、轮询机制)后的修订:

1. **§4.4 活跃描边几何修正**: 圆环(r = node-height/2 + 4)会与 140×44 矩形节点相交、视觉穿过节点体;改为节点外扩 4px 的同形圆角矩形描边。
2. **§5.2 状态编码双通道化**: `--color-success` 在 token 层就是 brand 的别名,running/completed 同色仅靠 8px dot 空心/实心区分,在 reduced-motion 下失效;改为 running = 空心 dot + 描边,completed = 实心 dot + `--color-brand-soft` 底色。
3. **§5.3 边配色**: 默认边从 hairline(暗色 8% white,过淡)提升为 `--color-border-strong`(16%);箭头与边同色、不再常亮 teal — teal 保留给"活跃/流动"语义。
4. **§5.2 glyph emoji 修正**: `✋` 默认 emoji 呈现,改为 `⏸`;所有 glyph 附加 U+FE0E 强制文本呈现。
5. **§8.1 token 修正**: `--ease-fast` 不存在(motion 体系只有 `--ease-out` 一个缓动);各行时长改标 `--dur-fast/--dur/--dur-slow`;补"节点崩溃闪烁"行;移除 MiniTopology 入场 stagger 行(§15.4 已取下,原文矛盾)。
6. **§9.3 脉冲去重**: 原方案在"上游 completed"和"下游 pending→running"两个 transition 都会对同一条边触发脉冲(2s 轮询常同帧观测,脉冲翻倍);改为只在 `*→completed` 触发出边脉冲,补 `pending→completed` 跳变处理;diff 同时派生本地时间线事件,解决 Phase 1 时间线只有终态事件、几乎为空的问题。
7. **§5.5 MiniTopology 大图省略**: 固定 80×24px 无法容纳 >8 节点,加中间链 `···` 折叠规则。
8. **§6.1 补齐**: 画布右上角状态图例(双通道编码的说明书);节点键盘可达(tabindex/role/Enter);事件时间线 Phase 1 用派生事件。
9. **事实修正**: graph 路由 5 种 → 4 种(对照 `useHashRoute.ts`);YAML 依赖明确为 `yaml` v2(非 js-yaml);§7.1 token 同步(新增 `--color-graph-node-fill-done`、`--color-graph-arrow-active`)。
10. **§5.2 节点名截断规则**: 140px 定宽容纳不下长节点名,加 ellipsis + tooltip 规则。
