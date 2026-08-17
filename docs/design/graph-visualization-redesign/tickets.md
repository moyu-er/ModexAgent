# Tickets: Graph 可视化信息架构重构

实现 ticket,基于 `PRD.md`(同目录)。工作 frontier:任何 blocker 已完成的 ticket 可立即开工。

## Phase 1 — 后端 spec 不可变 + 前端信息架构

## T01 — 后端: spec 不可变 (GraphSpecStore + DDL + 路由)

**What to build:** ADR-0040 change 3 的后端落地。`GraphSpecStore` ABC + InMemory + SQLite 三实现改为: `save` 始终 INSERT(移除 `ON CONFLICT DO UPDATE`);新增 `save_if_changed(spec)` 内容去重保存;`list_records()` 返回每个 name 的最新 `spec_id`(`MAX(spec_id) GROUP BY name`)。`graph_specs` DDL 移除 `UNIQUE(name, version)` 约束 + 移除 `trg_graph_specs_auto_updated_at` trigger(不可变行无 UPDATE)。`handle_put_spec` 改用 `save_if_changed`,移除 name/version 不可变检查,响应返回(可能新的)`spec_id`。`GraphSpecLoader` 改用 `save_if_changed`。

**Blocked by:** None — 后端独立,可立即开工。

**Files:**
- `src/modex_graph/spec_store.py` — `GraphSpecStore` ABC + `InMemoryGraphSpecStore` + `SqliteGraphSpecStore`
- `src/modex_agent/persistence/migrations/workspace/001_initial.sql` — `graph_specs` DDL
- `examples/bot_project/bot/webui/routes/graph_routes.py` — `handle_put_spec`
- `examples/bot_project/bot/graph/spec_loader.py` — 启动加载

- [ ] `GraphSpecStore.save` 始终 INSERT 新行(新 Snowflake spec_id);InMemory + SQLite 两实现同步
- [ ] `GraphSpecStore.save_if_changed(spec)`: 按 name 查最新(MAX spec_id),内容相同返回已有 spec_id,不同或不存在则 INSERT
- [ ] `GraphSpecStore.list_records()`: 返回 `MAX(spec_id) GROUP BY name` 的记录(每个 name 只一条最新)
- [ ] `graph_specs` DDL(001_initial.sql + SqliteGraphSpecStore._init_schema): 移除 `UNIQUE(name, version)`,移除 trigger
- [ ] `handle_put_spec`: 调用 `save_if_changed`,移除 name/version 不可变检查,响应返回新 spec_id
- [ ] `GraphSpecLoader`: 调用 `save_if_changed`(启动幂等)
- [ ] `python -m pytest tests -q` 相关套件 pass(spec_store + graph_routes + spec_loader)

## T02 — 前端: GraphSpecDetail 新组件 (拓扑 + instance 列表 + composer)

**What to build:** PRD §3 的 spec 详情视图。替换 `/graphs/:specId` 路由的 `GraphConversation`。布局: Header(Back + spec name + version + Edit YAML) + 主区拓扑预览(`TopologyCanvas`, 无状态着色, 纯结构) + 右侧 instance 列表(w=320, `listInstances?spec_id=`, 每行 instance ID + status badge + version 号 + 进度 + 时间) + 底部 composer(`POST /specs/{specId}/run` → 新建 instance → 跳转 instance 详情)。左侧 Sidebar 保留(App.tsx 不变)。

**Blocked by:** T01 — 前端需 `updateSpec` 返回新 spec_id 的行为;但组件本身可与 T01 并行开发(mock API)。

- [ ] `GraphSpecDetail.tsx` 新组件: Header + 拓扑预览 + instance 列表 + composer
- [ ] instance 列表数据: `listInstances(ws, undefined, specId)` (spec_id 过滤);点击行 → `navigate("/graphs/instances/:id")`
- [ ] composer: `runGraph(ws, specId, content)` → 成功后 `navigate("/graphs/instances/{new_instance_id}")`
- [ ] 拓扑预览: `getSpec(ws, specId)` → `parseGraphSpecYaml` → `TopologyCanvas`(无 nodeStatuses — 纯结构)
- [ ] "Edit YAML" 按钮 → `navigate("/graphs/:specId/edit")`
- [ ] `App.tsx` 路由: `/graphs/:specId`(graphConversation)→ `GraphSpecDetail`;移除 `GraphConversation` import
- [ ] i18n key 新增;组件测试(mock graphsApi)+ `npm run build` + `npm test` pass

## T03 — 前端: GraphInstanceDetail 新组件 (会话流 + composer + 拓扑抽屉)

**What to build:** PRD §4 的 instance 详情视图。替换 `/graphs/instances/:id` 路由的 `GraphExecutionViewer`(作为主体)。布局: Header(Back → spec 详情 + instance ID + spec name + spec version badge + status + Topology 按钮) + 主区会话流(每次 invocation = version 号 + user 气泡 + graph 输出气泡 + 内嵌 MiniTopology) + 底部 composer(`POST /instances/{id}/invoke`, 终态可用) + 拓扑抽屉(header 按钮 → slide-in panel, spec 拓扑 + 节点状态 + meta)。左侧 Sidebar 保留。

**Blocked by:** T02 — 路由结构调整;但组件可并行开发。

- [ ] `GraphInstanceDetail.tsx` 新组件: Header + 会话流 + composer + 拓扑抽屉
- [ ] 会话流数据: `list_by_instance` 的前端 API(需新增 `getInvocations(ws, instanceId)` → `GET /instances/{id}/events` 或专用端点)+ `getInstance(ws, instanceId)` 的 nodes 数组
- [ ] 每次 invocation 渲染: version 号 + 时间戳 + user 气泡(`.bubble-user`) + graph 输出气泡(`.bubble-assistant` + MarkdownRenderer + MiniTopology)
- [ ] running invocation: typing dots + 部分 MiniTopology 着色
- [ ] composer: `invokeInstance(ws, instanceId, content)` → 乐观更新 + 轮询/WS;终态可用,running 禁用
- [ ] 拓扑抽屉: slide-in panel(w=360), spec 拓扑(`getSpec(ws, instance.spec_id)`)+ 节点状态着色 + meta(scheduler/trigger/nodes/versions)
- [ ] `graphsApi.ts` 新增 `invokeInstance` + `getInvocations`
- [ ] `App.tsx` 路由: `/graphs/instances/:id`(`GraphExecutionViewer`)→ `GraphInstanceDetail`
- [ ] `useGraphExecution` hook 复用(轮询/WS + diff);会话流的实时更新
- [ ] i18n key 新增;组件测试 + `npm run build` + `npm test` pass

## T04 — 前端: 废弃 GraphConversation + /graphs/instances 路由

**What to build:** 清理废弃代码。删除 `GraphConversation.tsx` 及其测试。移除 `/graphs/instances` 全局路由(`useHashRoute.ts` 的 `graphInstances` 分支)。`GraphInstanceListPage.tsx` 组件代码可复用为 T02 spec 详情右侧 instance 列表的基础(或提取共享子组件)。更新 `App.tsx` 路由分支。

**Blocked by:** T02 — `GraphSpecDetail` 替换 `GraphConversation`;T03 — `GraphInstanceDetail` 替换 `GraphExecutionViewer`。

- [ ] 删除 `GraphConversation.tsx` + `GraphConversation.test.tsx`
- [ ] `useHashRoute.ts`: 移除 `graphInstances` route kind(`parseHash` 的 `instances` 分支)
- [ ] `App.tsx`: 移除 `graphInstances` 分支;`GraphSpecListPage` 的 "Instances" 按钮 → 改为 spec 详情内的 instance 列表
- [ ] `GraphInstanceListPage.tsx`: 提取 instance 行渲染为共享子组件(供 T02 spec 详情右侧列表复用),或整体删除
- [ ] `npm run build` + `npm test` pass(无残留引用)

## T05 — 前端: GraphSpecEditor spec_id 变化导航

**What to build:** `GraphSpecEditor` 保存后,`updateSpec` 返回的 `spec_id` 可能与当前不同(ADR-0040: 内容变化 = 新 spec_id)。保存成功后检查返回的 `spec_id`:如果变化 → `navigate("/graphs/{new_spec_id}")`;如果相同 → 保持。`GraphSpecResponse` 类型已有 `spec_id` 字段,无需新增。

**Blocked by:** T01 — 后端 `handle_put_spec` 返回新 spec_id。

- [ ] `GraphSpecEditor.tsx`: save 成功后检查 `result.spec_id !== specId` → navigate
- [ ] 测试: mock `updateSpec` 返回不同 spec_id → 验证 navigate 调用
- [ ] `npm run build` + `npm test` pass

## T06 — Phase 1 收尾 (a11y / reduced-motion / 双主题 / 文档)

**What to build:** 全量审计。键盘流(Tab 遍历 instance 列表行、Enter 进入、Esc 关闭拓扑抽屉)、reduced-motion 全量降级、双主题视觉 sweep(375/768/1024/1440)、token 无裸 hex、i18n key 无遗漏、`webui/AGENTS.md` 更新(graph 组件表 + 废弃组件 + 新组件)。

**Blocked by:** T02, T03, T04, T05。

- [ ] 键盘可完成"进入 spec 详情 → 选中 instance → 进入 instance 详情 → re-invoke"全流程
- [ ] reduced-motion 下无位移动画,信息零丢失
- [ ] 双主题四宽度 sweep
- [ ] `webui/AGENTS.md` graph 部分更新(新组件表 + 废弃说明)
- [ ] `npm run build` + `npm test` pass(全量无回归)

## Phase 2 — 实时事件 + 调度可视化降级

## T07 — ~~拓扑抽屉内嵌 GraphExecutionViewer~~(Rev 2 废弃)

**Status: 废弃(Rev 2)**。Rev 1 曾规划 `GraphExecutionViewer` 降级为拓扑抽屉内的二级视图。
Rev 2 推翻该决策(PRD §5.3):抽屉式拓扑使活图实际不可见,`GraphExecutionViewer` 的全画布
布局转正为 instance 详情主体(Rev 3 再次修订为 modal 内体验,见 PRD §5.3)。
**Rev 3 由 T09 的 Run Graph modal 承接**(360px 拓扑抽屉一并移除)。

## T08 — WS subscribe_graph 驱动会话流实时更新

**What to build:** `GraphInstanceDetail` 集成 WS 模式。进入 instance 详情 → `subscribe_graph` → `graph_event` 消息驱动节点状态更新 + invocation 状态 + 会话流实时刷新。WS 断开回退 2s 轮询。`useGraphExecution` WS 模式复用(G11 已实现)。

**Blocked by:** T03 — `GraphInstanceDetail` 组件。

- [ ] WS 模式下会话流实时更新(running invocation 的 typing dots → completed 输出文本)
- [ ] 节点状态 → MiniTopology 着色
- [ ] 断线回退轮询 + 重连重订阅
- [ ] 测试 + `npm run build` + `npm test` pass

---

## Phase 3 — 运行图弹窗 + 状态视觉系统(Rev 3)

> Rev 3(2026-08-14):T09/T11 按 Rev 3 PRD §4/§3.2 重写,原 Rev 2 版描述作废。

## T09 — Instance 详情:运行图弹窗(Run Graph modal)(PRD §4 Rev 3)

**What to build:** `GraphInstanceDetail` 保持现产线会话优先布局(header + invocation 会话流 +
底部 re-invoke composer,均不变);header 的 Topology 按钮改为打开**居中 Run Graph modal**
(默认不显示,不自动打开),modal 内承载 Variant A 完整体验:

- **modal 规格**:近全屏居中(如 `inset-6` / `max-w-[1200px]`,高 85vh,`bg-canvas-popover` +
  `shadow-card-hover`);`role="dialog"` + `aria-modal` + focus trap;Esc / ✕ / backdrop 关闭,
  关闭后焦点返还 Topology 按钮;小屏(≤768px)右栏下移堆叠
- **顶栏**:spec name · version + 状态 badge + Pause/Resume/Stop 控制组(复用
  `GraphExecutionViewer` 的 `canPause`/`canResume`/`canStop` 状态机,操作后 `refresh()`)+ ✕
- **主区**:全尺寸 `TopologyCanvas` — `nodeStatuses` + `activeEdges` + `pulses` + crash flash;
  agent 节点单击跳 session,非 agent 单击选中
- **右栏(w-80)**:选中节点 → `NodeDetailPanel`;默认 → `InstanceSummary`;下方
  `EventTimeline`(max-h 35%,接入 `useGraphExecution` 的 `timeline`);running/paused 时
  inline Deliver 面板(`DropdownPanel` + textarea + Send)

**删除清单**:`TopologyDrawer`(360px 右抽屉)删除;`GraphExecutionViewer.tsx` 的控制状态机/
侧栏/Deliver/elapsed 迁移入 modal,迁移完成后组件与其测试删除(测试迁入 modal 组件);
`useGraphExecution` 解构补 `timeline`/`crashFlashes`(当前产线 instance 页未消费,T09 接线)。

**Blocked by:** None — 纯前端,可与 T10 并行(文件集不相交)。

**Files:**
- `webui/src/components/graphs/GraphInstanceDetail.tsx` — Topology 按钮 → Run Graph modal;删除 TopologyDrawer
- `webui/src/components/graphs/GraphExecutionViewer.tsx`(+ `.test.tsx`)— 代码迁移后删除,测试迁入 modal
- `webui/src/components/graphs/detail/`(NodeDetailPanel / InstanceSummary / EventTimeline)— 复用,不改动
- `webui/src/i18n/en.ts` — i18n 约定:控制/deliver/时间线等复用已有 key(随 `GraphExecutionViewer` 迁移);**零新增 key** — header 按钮复用 `graphs.topology`,modal aria-label 复用 `graphs.drawerTitle`(不随抽屉删除);`graphs.newInstance` + `graphs.time*` 由 T11 新增(en 英文)

- [ ] modal 开合:Topology 按钮打开;Esc / ✕ / backdrop 关闭,焦点返还触发按钮;默认不自动打开
- [ ] 顶栏:Pause/Resume/Stop 状态机(canPause/canResume/canStop),操作后 `refresh()`
- [ ] 主区画布:nodeStatuses + activeEdges + pulses + crashFlashes 全量接入;agent 节点单击跳 session,非 agent 单击选中
- [ ] 右栏:未选中 → `InstanceSummary`;选中节点 → `NodeDetailPanel`;`EventTimeline`(max-h 35%)接入 `timeline`;running/paused 时 inline Deliver 面板
- [ ] 数据:modal 打开期间 WS/轮询照常驱动;关闭后页面 badge/气泡进度继续实时
- [ ] 小屏(≤768px):modal 右栏下移堆叠
- [ ] 删除 `TopologyDrawer` + `GraphExecutionViewer.tsx`(测试迁移,无残留引用)
- [ ] `npm run build` + `npm test` pass

## T10 — 状态视觉系统(PRD §6)

**What to build:** 状态色硬伤修复。`--color-graph-status-*` 6 token **已随原型入库
`index.css`**(`:root`/`.dark` 双主题,`index.tokens.test.ts` 用例已在,验证即可;不改全局
`--color-success`);`TopologyCanvas` 右上角图例改为彩色
chip(每状态真实色圆点 + `text-body` 标签,crashed 用 `✕` 字形,补 suspended/canceled);
`GraphNode` 整节点双通道着色(completed 绿描边+绿 tint、crashed 红描边+红 tint、canceled
名称删除线);`GraphStatusBadge` 升级为带底色 chip;`MiniTopology.MINI_STATUS_FILL` 同步新 token。

**Blocked by:** None — 与 T09 并行派发(Rev 3 执行顺序:T09 + T10 并行 → T11 → T12;T09 触碰 GraphInstanceDetail/modal,T10 触碰着色,文件交集仅 GraphNode/TopologyCanvas 的 props 面,同 PR 需分两 commit)。

**Files:**
- `webui/src/index.css` — `--color-graph-status-*` tokens(`:root` + `.dark`)— 已随原型入库,验证
- `webui/tailwind.config.js` — token → utility 映射(仅新增,不改已有)
- `webui/src/components/graphs/topology/TopologyCanvas.tsx` — 图例彩色 chip 化
- `webui/src/components/graphs/topology/GraphNode.tsx` — `STATUS_STYLES` 双通道升级
- `webui/src/components/graphs/shared.tsx` — `GraphStatusBadge` 带底色
- `webui/src/components/graphs/topology/MiniTopology.tsx` — `MINI_STATUS_FILL` 同步

- [ ] tokens 已在 `index.css` 双主题(原型附带,验证);无裸 hex 出现在组件;`index.tokens.test.ts` 用例已在
- [ ] 图例:6 状态彩色 chip,文字 `text-body`,双主题 sweep 可辨
- [ ] 节点:completed 绿描边+tint、crashed 红描边+tint、canceled 删除线、running 保留呼吸环
- [ ] `GraphStatusBadge` 带底色,pending/stopped 灰度分层(stopped 删除线)
- [ ] `MiniTopology` 同步;reduced-motion 下静态双通道仍可分辨
- [ ] 组件测试更新(GraphNode/TopologyCanvas/shared)+ `npm run build` + `npm test` pass

## T11 — Spec 详情:新建实例弹窗 + 实例行重设计(PRD §3.2 C/D Rev 3)

**What to build:** ① **新建实例**:移除 spec 详情右栏底部的 textarea + Run composer(拥挤、
不美观);改为主区右下角 FAB(＋,56px 圆形 primary,绝对定位 `right-6 bottom-6`,`aria-label`)
→ **New Instance 居中弹窗输入框**(原 composer 的独立版,类似会话"新建会话"能力):
- modal 内容:spec name + version 标签 + `.composer` 风格 textarea(autofocus;Enter = Run,
  Shift+Enter 换行)+ Run 按钮
- 提交:`runGraph` → `navigate("/graphs/instances/:id")`(行为与旧 composer 一致);
  错误在弹窗内展示
- 取消:Esc / ✕ / backdrop;提交中禁用输入与按钮

② **实例行重设计**:`GraphSpecInstanceRow` — `#id`(mono)+ 升级版彩色 badge(T10)+
进度/耗时(`2/3 · 12s`)+ 相对时间 + MiniTopology 节点状态着色(数据已在 `nodeStatuses`
map);hover/active 态与 Sidebar 会话行一致(`--color-session-hover/active`)。

**Blocked by:** T10 — 依赖升级后的 badge 与 token。

**Files:**
- `webui/src/components/graphs/GraphSpecDetail.tsx` — 移除底部 composer;新增 FAB + New Instance modal
- `webui/src/components/graphs/GraphSpecInstanceRow.tsx` — 行重设计
- `webui/src/i18n/en.ts` — 新增 key(en):`graphs.newInstance`("New Instance")、modal 标题/placeholder/FAB aria-label 等

- [ ] FAB:主区右下角 56px 圆形 primary,仅 icon,`aria-label` 完整
- [ ] New Instance modal:autofocus;Enter = Run / Shift+Enter 换行;Esc/✕/backdrop 取消;提交中禁用;错误弹窗内展示
- [ ] 提交 `runGraph` → 跳转 instance 详情(与旧 composer 行为一致)
- [ ] 实例行:`#id` mono + 新彩色 badge + 进度/耗时 + 相对时间 + MiniTopology 状态着色 + hover/active(session token)
- [ ] 测试(FAB 开弹窗、modal 提交/取消/Esc、实例行渲染)+ `npm run build` + `npm test` pass

## T12 — Phase 3 收尾(a11y / reduced-motion / 双主题 / 文档)

**What to build:** 全量审计。a11y 键盘流(Run Graph modal 与 New Instance modal 的 focus
trap、Esc,Tab 遍历画布节点/实例行/弹窗控件)、reduced-motion 全量降级信息零丢失(呼吸环/
脉冲关闭时双通道静态着色仍可分辨)、双主题(明/暗)× 宽度(375/768/1024/1440)视觉 sweep
修正、新 token 无裸 hex、i18n key 无遗漏(全英文)、`webui/AGENTS.md` graph 组件表更新
(`TopologyDrawer`/`GraphExecutionViewer` 删除 + Run Graph modal 新结构)。

**Blocked by:** T09, T10, T11。

- [ ] 键盘可完成"打开 instance → 开/关 Run Graph modal → 选中节点 → 打开 session → re-invoke"全流程;焦点管理(focus trap、关闭后返还触发按钮)
- [ ] reduced-motion 全量降级,信息零丢失
- [ ] 双主题四宽度 sweep(重点:图例 chip、节点 tint、badge 底色对比度)
- [ ] `webui/AGENTS.md` graph 部分更新
- [ ] `npm run build` + `npm test` pass(全量无回归)
