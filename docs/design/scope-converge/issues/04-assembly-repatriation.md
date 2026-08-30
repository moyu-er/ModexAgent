# 04 装配代码搬家：从 bot_project 收回框架

Labels: wayfinder:deliberate
Status: closed
blocked-by: 03
closed-by: deliberate session 2026-08-18 (Q9-Q12 搬家决议)

## Question

现在装配散在 `examples/bot_project` 的 builders.py / pool/factory.py / wiring.py / memory_defaults.py——这是"点名册成立"的前提工程（用户已定方向：bot_project 降格为默认组件包 + 默认点名册）。

要决定：

- 搬家清单与边界：哪些必须进框架（统一装配器、默认 memory/experience/dream 反射弧、默认组件包），哪些留在业务（QQ/Telegram 适配器、WebUI、eval、SkillsStore）。
- 搬家后 bot_project 的目录形态与"参考消费者"的新示范方式。
- experience/dream 的反射弧收回框架后，"模型未配置就静默跳过"的现状如何消除。

## Comments

### Deliberate 决议（2026-08-18，随 03 闭合）

03 的 Q9-Q12 决议直接回答了 04 的三个问题。

#### 搬家清单与边界（对应 Q11）

**必须进框架**（管道 stage 包装已有 ABC）：
- `ResourceFactory[R]` 的默认实现（单 workspace + 多 live 两种）→ `WorkspaceMaterializeStage`
- `ExecutionStrategy` 的 react + external 两个实现 → `PoolAssembleStage` 内部 dispatch
- memory 系统构建反射弧（`build_pool_data`）→ `BuildMemoryStage`
- governance 派生反射弧（`create_governance`）→ `BuildGovernanceStage`
- hook 注册反射弧（当前 main 在 BIZ `pipeline_wiring.py`，sub 在 FW `template.py`）→ `BuildHooksStage`
- interceptor chain 构建（`InterceptorChain`）→ `BuildInterceptorsStage`
- experience wiring（`ExperienceReviewHook`）→ `BuildHooksStage` 内
- approval runtime（`ApprovalRuntime`）→ `BuildGovernanceStage` 内
- memory 预设（`config/memory_defaults.py` 的三个 preset 函数）→ 框架默认 plugin

**留在业务**：
- QQ/Telegram/WebUI 适配器（`bot/adapters/`）
- WebUI server（`bot/webui/`）
- SkillsStore CRUD（`bot/...`）
- tool/MCP 构建中的业务部分（`resolve_system_prompt`）
- `builders.py` 的业务 tool factories（通用 part 进框架，业务 part 留）

#### bot_project 降格形态（对应 Q4 + Q9）

bot_project 终局 = 默认组件包 + 默认点名册：
- **默认组件包**：框架提供的默认 plugins（react strategy、external strategy、单/多 workspace factory、memory defaults、experience reviewer、loop detection、current time、knowledge、run logging、tool timeout interceptor）
- **默认点名册**：`config/pools/` + `templates/` 的 YAML 文件，引用默认组件包中的组件名
- **业务保留**：适配器、WebUI、SkillsStore、业务特定 tool factories

装配反射弧收回框架后，`create_pool`（BIZ `pool/factory.py`）变成对 `AssemblyPipeline.run(spec, ctx)` 的调用——不再内联装配逻辑。`AgentTemplate.materialize`（FW `template.py`）同样变成对管道的调用（subagent stage 子集）。两条路径在管道收敛。

#### experience/dream 反射弧收回（对应 Q9 + Q12）

当前"模型未配置就静默跳过"的现状（`wiring.py:534` 的 `pipeline is None` guard）由 Q12 的触发机制 plugin 化消除：`experience_review` plugin 在 roster 里显式启用/禁用，不再依赖隐式的"模型未配置"检测。plugin 配置携带 `enabled`/`min_messages`/`cooldown_turns`，框架默认 plugin 提供合理默认值。

dream engine / compactor / consolidator 同理——各自成为框架默认 plugin，roster 显式控制启用。

#### factory 形注册（对应 Q9）

02 决议延后的"工厂形状 builtins（TodoContinuation/ControlDrain/ExperienceReview/TurnOutcomeNotify/SubagentAutoSend）等 04 的装配器支持 factory 形注册"——Q9 的管道形态支持：factory 形 stage 在 `process(spec, ctx)` 时根据 spec 的组件名引用 + config 从 ComponentRegistry 解析工厂，再调用工厂创建实例。ComponentRegistry 存储工厂实例，管道 stage 调用工厂的 `create(config) -> Component` 方法。

### 依赖

- 03 已闭 → 04 可实施
- 04 的实施依赖 Q9 管道基础设施（AssemblyPipeline/Stage/Spec/Context）先就位
- 建议分阶段：阶段 1（管道基础设施）→ 阶段 2（收敛 agent 装配）→ 阶段 3（roster + 配置）→ 阶段 4（特殊 agent + 触发）→ 阶段 5（迁移 + 测试）
