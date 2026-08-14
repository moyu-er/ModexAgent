# 10 — WebUI graph visual config

Status: ✅ design closed (直接实现设计,无待确认项)
Labels: wayfinder:active
Blocking: 无

## Question

**WebUI 前端: GraphSpec YAML 编辑器 + 图执行查看器 + agentNode 跳转 session。**

### 上下文(基于代码探索确认)

**WebUI 前端**: React(`examples/bot_project/webui/`)
**后端 API**: ticket 09 已设计完整 REST 端点
**参考**: 现有 WebUI 的 PoolConfig 页面(pools CRUD + YAML 编辑)

## Discussion

### 1. 两种编辑模式

#### 模式 A: YAML 编辑器(本期实现)

- 打开 GraphSpec 编辑页面
- 加载 YAML 内容(`GET /api/graphs/specs/{spec_id}` → `yaml_content`)
- Monaco editor / textarea 编辑
- 保存时 `PUT /api/graphs/specs/{spec_id}` → 后端校验(parse + compiler.validate)
- 校验失败 → 显示错误信息(不保存)
- 校验通过 → 保存成功提示

**校验错误展示**: 后端返回 `{"error": "Invalid graph spec: ..."}`,前端在编辑器下方显示错误面板(行号 + 错误信息)。

#### 模式 B: 可视化编辑器(后续增强)

- 拖拽节点(node palette → 画布)
- 连接节点(画边)
- 节点配置面板(agent_name / pool_name / trigger 等)
- 保存时: 前端把画布状态序列化为 YAML → 走模式 A 的保存路径(PUT + 校验)

**注**: 模式 B 是后续增强,本期只做模式 A。两者共享保存 API(PUT + 校验),模式 B 只是前端的另一种编辑 UI。

### 2. 图执行查看器

**图列表页**:
- `GET /api/graphs/specs` → 列出所有 GraphSpec(名称 + version)
- 每个 spec 有"运行"按钮 → 弹输入框 → `POST /api/graphs/specs/{spec_id}/run`

**实例列表页**:
- `GET /api/graphs/instances` → 列出图实例(status 标签: running/completed/crashed/paused)
- 点击实例 → 进入实例详情页

**实例详情页**(执行查看器):
- `GET /api/graphs/instances/{id}` → 节点状态列表
- 节点状态渲染: 每个节点显示 status(PENDING/RUNNING/COMPLETED/CRASHED)+ invocation_id
- agentNode 跳转: 点击 agentNode → 跳转到 `/sessions/{session_id}`(session_id = `nodeId.agentName`, ticket 05 §3.4)
- 图结果: completed 时显示 `result`(END 节点聚合的 list)

**实时更新**: WebSocket 推送(ticket 11 §4)
- 图执行中: agent 事件流(WebBotEmitter → WebSocket → 前端实时渲染)
- 图完成: `GraphOutput(kind=COMPLETED)` → 前端更新状态 + 显示结果
- 图崩溃: `GraphOutput(kind=CRASHED)` → 前端更新状态 + 显示错误

### 3. agentNode 跳转 session

**URL**: `/sessions/{session_id}`

**session_id 格式**: `nodeId.agentName`(ticket 05 §3.4 的 CACHED 模式映射)

**跳转逻辑**:
1. 实例详情页渲染节点列表
2. agentNode 有 `node_id` + `agent_name`(从 NodeStatusInfo 获取)
3. 点击 → `window.location.href = /sessions/${nodeId}.${agentName}`
4. session 页面加载该 session 的对话历史 / transcript

**注**: 需要确认 session 页面是否已存在(bot_project 现有 WebUI)。如果不存在,需要新建——但这是 bot 前端工作,不是框架设计问题。

### 4. 图控制按钮(实例详情页)

- pause: `POST /api/graphs/instances/{id}/pause`(running 时可用)
- resume: `POST /api/graphs/instances/{id}/resume`(paused 时可用)
- stop: `POST /api/graphs/instances/{id}/stop`(running/paused 时可用)
- deliver: 弹输入框 → `POST /api/graphs/instances/{id}/deliver`(running/paused 时可用)

### 5. 前端组件结构

```
webui/src/pages/graphs/
├── GraphSpecListPage.tsx       # 图 spec 列表 + 运行按钮
├── GraphSpecEditPage.tsx       # YAML 编辑器(模式 A)
├── GraphInstanceListPage.tsx   # 图实例列表
├── GraphInstanceDetailPage.tsx # 执行查看器(节点状态 + 控制按钮 + 跳转)
└── components/
    ├── NodeStatusCard.tsx      # 单节点状态卡片
    ├── GraphControlPanel.tsx   # pause/resume/stop/deliver 按钮
    └── YamlEditor.tsx          # Monaco/textarea 编辑器 + 校验错误面板
```

### 6. 不依赖后端新设计

ticket 09 的 REST API 已完整覆盖 10 的需求。10 是纯前端实现,不新增后端设计问题。

## 归属

全部 **bot_project**(纯前端 React)。

| 层 | 内容 | 归属 |
|----|------|------|
| GraphSpecListPage | spec 列表 + 运行 | **bot_project** |
| GraphSpecEditPage | YAML 编辑器(模式 A) | **bot_project** |
| GraphInstanceListPage | 实例列表 | **bot_project** |
| GraphInstanceDetailPage | 执行查看器 + 跳转 + 控制按钮 | **bot_project** |
| YamlEditor + 校验错误面板 | 编辑器组件 | **bot_project** |
| 可视化编辑器(模式 B) | 拖拽点/边 | **bot_project**(后续增强) |

## 不做什么

- 不做可视化拖拽编辑器(模式 B)——后续增强
- 不做 GraphSpec 的 POST 创建 / DELETE 删除 UI——YAML 文件管理为主
- 不做图执行的实时拓扑高亮(节点变色)——后续增强,依赖 WebSocket 推送节点状态变更事件
- 不在框架层新增任何东西——纯前端
