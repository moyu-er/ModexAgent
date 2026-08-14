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

## T07 — 拓扑抽屉内嵌 GraphExecutionViewer (调度可视化二级视图)

**What to build:** PRD §5.3。`GraphInstanceDetail` 拓扑抽屉中的"查看调度详情"按钮 → 展开 `GraphExecutionViewer` 的全画布调度可视化(deliver 脉冲 + 节点详情 + 事件时间线 + deliver 面板)。作为调试用途的二级视图,不是日常路径。

**Blocked by:** T03 — `GraphInstanceDetail` 拓扑抽屉。

- [ ] 拓扑抽屉内"调度详情"按钮 → modal/全屏 `GraphExecutionViewer`
- [ ] `GraphExecutionViewer` 组件保留,作为二级视图入口
- [ ] 测试 + `npm run build` + `npm test` pass

## T08 — WS subscribe_graph 驱动会话流实时更新

**What to build:** `GraphInstanceDetail` 集成 WS 模式。进入 instance 详情 → `subscribe_graph` → `graph_event` 消息驱动节点状态更新 + invocation 状态 + 会话流实时刷新。WS 断开回退 2s 轮询。`useGraphExecution` WS 模式复用(G11 已实现)。

**Blocked by:** T03 — `GraphInstanceDetail` 组件。

- [ ] WS 模式下会话流实时更新(running invocation 的 typing dots → completed 输出文本)
- [ ] 节点状态 → MiniTopology 着色
- [ ] 断线回退轮询 + 重连重订阅
- [ ] 测试 + `npm run build` + `npm test` pass
