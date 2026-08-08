# Tickets: Graph 可视化与调度过程展示

Vertical slices implementing the graph visualization redesign. Source spec: `PRD.md` (same directory, Rev 2) — section references below point there.

Work the **frontier**: any ticket whose blockers are all done. G01 / G04 / G09 可以立即并行开工;G02/G03 随后;G05 是 Phase 1 的收口;G08 收尾。Phase 2(G09~G12)与 Phase 1 后端无冲突,可穿插进行。

## Phase 1 — 纯前端(无后端改动)

## G01 — Token 扩展 + 依赖 + YAML 解析 + dagre 布局

**What to build:** 图可视化的地基。`index.css` 增加 §7.1 全部 graph 语义 token(含 `--color-graph-node-fill-done`、`--color-graph-arrow-active`)+ §7.2 motion token,`tailwind.config.js` 增加映射;安装 `@dagrejs/dagre`、`yaml`;实现 `components/graphs/yaml/parseGraphSpec.ts`(YAML → `ParsedGraphTopology`,§9.2)与 `components/graphs/topology/layout.ts`(dagre 封装,spec → 坐标,§9.2 `LayoutResult`)。

**Blocked by:** None — can start immediately.

- [x] `:root` / `.dark` 增加全部 graph token,全部映射既有 token,无新色值;token 完整性测试覆盖
- [x] `parseGraphSpec.ts` 正确解析 `config/graphs/simple.yml` 与 `review_workflow.yml`(含 `__start__`/`__end__` 补全、回环边);解析失败抛出带行号的结构化错误
- [x] `layout.ts` TB 布局,`nodesep: 40` / `ranksep: 60`;节点不重叠、边路径含回环(reviewer→implementer)
- [x] parse + layout 单元测试(两个真实 spec + 非法 YAML + 未知 node_type)
- [x] `npm run build` and `npm test` pass

## G02 — 拓扑组件族(TopologyCanvas / GraphNode / GraphEdge / MiniTopology)

**What to build:** §5 的静态视觉语言落地为 SVG 组件:`TopologyCanvas`(viewBox 自适应、滚轮缩放 0.5x–2x、拖拽平移、状态图例叠加)、`GraphNode`(§5.2 解剖:glyph + name + sub + status dot + 双通道状态着色 + 外扩矩形描边槽位;`tabindex="0"`/`role="button"`/Enter 选中;长名 ellipsis + title)、`GraphEdge`(§5.3:border-strong 边 + 同色箭头 + 高亮态)、`MiniTopology`(§5.5:80×24px、状态着色、>8 节点 `···` 折叠)。glyph 全部附加 U+FE0E。

**Blocked by:** G01 — Token 扩展 + 依赖 + YAML 解析 + dagre 布局。

- [x] 四种组件按 §5.2/§5.3/§5.5 渲染 review_workflow 五节点五边拓扑(含回环),双主题截图核对
- [x] 状态着色双通道:running = 空心 dot + 描边槽位,completed = 实心 dot + brand-soft 底色;crashed/pending/canceled 按表
- [x] 画布缩放/平移可用;图例按 §6.1 渲染;键盘 Tab/Enter 选中节点,focus ring 可见
- [x] MiniTopology 在 ≤8 / >8 节点两种输入下正确(折叠规则生效)
- [x] 组件测试(渲染状态矩阵 + MiniTopology 折叠)+ `npm run build` and `npm test` pass

## G03 — 动效组件(DeliverPulse / ActiveNodeRing + 降级)

**What to build:** §4 签名元素:`DeliverPulse`(brand-bright r=4 圆点沿边 path 移动 600ms + 24px 渐隐尾迹 + 12% glow,多边并发)与 `ActiveNodeRing`(节点外扩 4px 同形圆角矩形描边,1.2s opacity 0.3→0.6→0.3 脉动)。全部实现 §8.2 的 `prefers-reduced-motion` 降级(脉冲→边 220ms 高亮;描边→静态 40%)。

**Blocked by:** G02 — 拓扑组件族。

- [x] 脉冲沿 dagre 边 path 精确移动(source→target 方向正确),600ms `--ease-out`,结束自动卸载;多条边并发不阻塞
- [x] 活跃描边是矩形外描边(不是圆环),只在 running 节点出现,节点状态离开 running 即消失
- [x] reduced-motion 下:无位移动画,边高亮 + 静态描边,信息不丢失(§8.2 审计)
- [x] 动效用 transform/opacity/SVG 属性,不触发布局;组件测试(状态机 + 降级分支)
- [x] `npm run build` and `npm test` pass

## G04 — useGraphExecution hook(轮询 + diff + 派生事件)

**What to build:** §9.3 的执行状态管理 hook:2s 轮询 `getInstance` + `getEvents`(pending/running/paused/crashed 持续,终态停),`diffNodeStatuses` 按 §9.3 转换表产出 transition(含 `pending→completed` 跳变),transition → 脉冲触发(只在 `*→completed` 的出边)+ 节点着色 + **派生时间线事件**(node_started/completed/crashed,客户端时间戳,标记为推断)。纯逻辑(diff/transition 表)与 React 生命周期分离,纯逻辑可单测。

**Blocked by:** None — graphsApi 已存在,可与 G01 并行。

- [x] diff 转换表全覆盖(§9.3 五行)单测;同帧多 transition 不重复触发脉冲
- [x] 轮询生命周期正确(终态停止、卸载清理、instance 切换重置)
- [x] 派生事件进入时间线数据结构,与 REST events 合并去重
- [x] hook 测试(mock fetch)+ `npm test` pass

## G05 — GraphExecutionViewer 重构(核心视图)

**What to build:** §6.1 全量:顶部控制条(Back / instance id / 状态徽章 / Pause/Resume/Stop/**Deliver**)+ 全画布拓扑(消费 G02/G03/G04)+ 底部摘要条(进度/耗时/调度器/触发模式)+ 上下文侧栏三模式(节点详情 / 实例摘要含**图级 result 展示** / 事件时间线)+ `DeliverDialog`(节点选择 + 内容输入 + 提交 + 成功 toast)。单击选中、双击 agent 节点跳 session。

**Blocked by:** G02 — 拓扑组件族;G03 — 动效组件;G04 — useGraphExecution hook。

- [x] 布局按 §6.1:画布 flex-1 + 320px 侧栏 + 底部摘要条;小屏(≤768px)降级为列表+缩略图
- [x] 执行中可观察到:节点依次激活(描边)、deliver 脉冲沿边流动、completed 节点底色沉淀
- [x] Deliver 全流程可用(`deliverToNode` 首次有 UI);控制按钮可用性状态机保留
- [x] 侧栏三模式切换正确;实例 completed 时展示图级 result;agent 节点双击/按钮跳 `{node_id}.{node_name}` session
- [x] 全部新文案走 i18n catalog(§16);集成测试(mock graphsApi)+ `npm run build` and `npm test` pass

## G06 — 列表页重构(Spec 列表 + 实例列表)

**What to build:** §6.3/§6.4:`GraphSpecListPage`(原 GraphConfigPage 重命名)每行 MiniTopology + name + version + 节点数/调度器/触发模式;`GraphInstanceListPage`(原 GraphListPage 重命名)每行状态着色 MiniTopology + instance id + spec 名 + 状态徽章 + 进度 + 耗时,状态过滤保留。实例列表按需并行拉取关联 spec 拓扑。App.tsx import 更新。

**Blocked by:** G02 — 拓扑组件族。

- [x] 两个列表页按 §6.3/§6.4 渲染;MiniTopology 数据来自 spec 拓扑解析(实例列表按 spec_id 关联)
- [x] 空状态文案按 §16("Add a YAML file to config/graphs/…")
- [x] 重命名后路由/导航/测试引用全部更新,无残留旧名
- [x] `npm run build` and `npm test` pass

## G07 — Spec 编辑器重构(CodeMirror + 实时预览分栏)

**What to build:** §6.2:左栏 `YamlCodeEditor`(CodeMirror 6 lazy import,YAML 高亮、行号、活跃行、Teal & Ember 主题、lint gutter),右栏上半拓扑实时预览(编辑器内容防抖 300ms → parse → TopologyCanvas,解析失败保持上次有效预览 + 错误提示),右栏下半运行区(user input + Run + 保存状态)。Save 后端校验错误映射为行级 lint marker + 错误面板。≤768px 切 tab。

**Blocked by:** G01 — parse/layout;G02 — 拓扑组件族(预览复用 TopologyCanvas)。

- [x] CodeMirror 懒加载,不影响 chat 首屏 bundle;YAML 高亮/行号/主题符合 §6.2
- [x] 编辑 → 300ms 防抖 → 预览实时更新;非法 YAML 不崩预览
- [x] Save 校验错误带行号,lint marker 与错误面板联动;Save/Run 流程完整(Run 后跳执行查看器)
- [x] 小屏 tab 切换可用;组件测试 + `npm run build` and `npm test` pass

## G08 — Phase 1 收尾(a11y / reduced-motion / 双主题 / 文档)

**What to build:** 全量审计与打磨:键盘流(Tab 遍历节点、Enter 选中、Esc 取消选中/关 Dialog)、reduced-motion 全量降级审计(§8.2)、双主题视觉 sweep(375/768/1024/1440)、token 无裸 hex/`/alpha` 修饰检查(T07 教训:`/alpha` 在 var() token 上不生成 CSS)、i18n key 无遗漏、`webui/AGENTS.md` 与 PRD Status 更新。

**Blocked by:** G05 — 执行查看器;G06 — 列表页;G07 — Spec 编辑器。

- [x] 键盘可完成"选中节点 → 打开详情 → 跳转 session"全流程;focus ring 处处可见
- [x] reduced-motion 下无任何位移动画,状态信息零丢失
- [x] 双主题四宽度 sweep 记录进 PRD;无裸 hex、无 `/alpha` 修饰符
- [x] `webui/AGENTS.md` graph 部分更新;PRD Status → implemented(Phase 1)
- [x] `npm run build` and `npm test` pass(全量 563+ 测试无回归)

## Phase 2 — 后端实时事件(增强)

## G09 — modex_graph 节点级事件扩展

**What to build:** §11.1:`GraphOutputKind` 增加 `node_started`/`node_completed`/`node_crashed`/`deliver_dispatched`;`GraphOutput` 增加 `node_id`/`node_name`/`invocation_id`/`target_node_id`/`timestamp`(frozen Pydantic,不破坏既有字段);发射点落在 `Node.run()` 生命周期与 `GraphPersistenceCoordinator.route_deliver()`。框架层改动,遵守 `rules/type-safety.md`。

**Blocked by:** None — 后端独立,可与 Phase 1 并行。

- [x] 四种事件在正确生命周期点发射(单元测试:mock adapter 断言 emit 序列)
- [x] `deliver_dispatched` 携带 source/target node_id;所有事件带 epoch ms timestamp
- [x] 既有图调度测试无回归;`GraphOutput` 序列化兼容既有 event_store 消费方
- [x] `python -m pytest tests -q` 相关套件 pass

## G10 — bot WS 订阅协议 + adapter 双通道

**What to build:** §11.2(已与现有 WS 基础设施对齐的方案):`WebSocketAction` 增加 `subscribe_graph`/`unsubscribe_graph`;新子模块 `routes/websocket/graph.py`(handler + 转发循环);`_WsConnectionState` 增加 `subscribed_graphs` + task 管理,`cleanup()` 统一注销;workspace 资源增加 `graph_event_subscribers`;`WebUIGraphOutputAdapter.emit()` 收敛为"写 event_store + fan-out 订阅队列"单点。消息形状 `{type: "graph_event", graph_instance_id, event}`。

**Blocked by:** G09 — modex_graph 节点级事件扩展。

- [x] 订阅/退订/断连三路径的队列注册与清理正确(无泄漏、无向已断连 ws 推送)
- [x] 运行中图实例的节点级事件实时到达订阅客户端;未订阅 instance 不推送
- [x] REST `/events` 轮询路径行为不变(双通道同源)
- [x] `tests/webui/` 新增 WS 订阅测试(aiohttp TestClient)+ 既有测试 pass

## G11 — webui WS 模式(替代轮询 + 真实时间线)

**What to build:** `useGraphExecution` 增加 WS 模式:进入执行查看器时 `subscribe_graph`,`graph_event` 消息在 App 层分流(不进 chat reducer),事件驱动节点状态/deliver 脉冲(精确 source→target)/时间线(真实 timestamp);WS 断开自动回退 2s 轮询,重连后重新订阅。派生事件与真实事件同槽位替换。

**Blocked by:** G05 — 执行查看器;G10 — WS 订阅协议。

- [x] WS 模式下脉冲精确落在真实 source→target 边;节点状态毫秒级更新
- [x] 时间线事件带后端 timestamp;派生事件被真实事件替换不重复
- [x] 断线回退轮询 + 重连重订阅,状态不丢不错乱
- [x] hook 测试(mock WS + mock fetch 双模式)+ `npm test` pass

## G12 — (可选)topology 端点 + 节点中间结果

**What to build:** §11.3/§11.4:`GET /api/graphs/specs/{spec_id}/topology` 返回编译后结构化拓扑(前端可卸 `yaml` 解析);`NodeStatusInfo` 扩展 `result` 摘要,侧栏节点详情展示节点输出(需截断策略防大 payload)。

**Blocked by:** G09 — modex_graph 节点级事件扩展。优先级低,Phase 2 末尾按需求决定是否实施。

- [x] topology 端点返回 compiler 校验后的 nodes/edges/scheduler,前端切换为端点数据源
- [x] 节点 result 有长度上限与截断展示
- [x] 后端路由测试 + 前端适配测试 pass
