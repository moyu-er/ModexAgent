# 长期记忆层启用(评测面裁定)

Status: closed
Labels: wayfinder:grilling
Blocks: 记忆探针套件 (11), E2E 记忆哨兵与消融臂 (14)
Resolved: 2026-08-20 — 选项 A 落定:仅评测启用,harness 显式驱动 Dream,生产默认关

## Question

跨会话长期记忆层(Archive / Core Memory / Dream Engine,当前**默认全部关闭**)是否纳入本轮启用并冒烟?

背景事实(2026-08-20 二次探查钉死):

- 启用面确认:pool.yml `memory: MemoryToggle`(multi_agent/pool_config/specs.py:65-86;archive_enabled/core_enabled,AND 门:core 需 archive,校验器 :79-86)→ `stack.py:68-82` `main_agent_memory(archive_enabled=, core_enabled=)` → presets.py:50-52(仅双开时派生 `DreamEngineConfig(enabled=True)`)
- **DreamEngine 仅在 bot/workspace/background.py:67-111 实例化**(后台轮询 workspace-dream 任务,扫 pool 找 archive+core 双开的);框架自身从不实例化
- **子代理构造上只有 session 层**(`build_session_only_memory`,template.py:232)—— 长期层是 main-agent 形状的记忆系统才有的
- **出厂 pool.yml 无 memory 块**(default/coder 均无)= archive/core/dream 全关;WebUI PoolEditor 有开关 UI(镜像 AND 门)
- CoreMemoryConsolidator 在 ioc/factories/memory.py:143-148 构建(core 开 + provider 在);`ensure_long_term_defaults`(pool_construction.py)在 core 开时播种 SOUL/USER/MEMORY 默认文件
- eval harness 直调 `main_agent_memory`(agent_harness.py:313)—— 评测启用 = preset 传参,**DreamEngine 需 harness 显式驱动**(非后台间隔)—— 对探针确定性反而更优(显式控制巩固时机)
- 已有机制完整:ScopedArchiveMemoryManager(cursor 消费)、ScopedCoreMemoryManager(检索策略+自动巩固)、DreamEngine(archive→core)—— 启用是配置+冒烟,不是新开发

选项:

- **A(推荐):纳入**。先落 09/10 插桩(对已运行部分立即产数)→ 本票启用长期层(评测配置 preset + 冒烟:启用后跑通一次真实摄入→巩固→注入链)→ 11 探针才有完整评测面。**生产默认仍关** —— 评测数据说话之前不改产品默认,这正是评测体系存在的意义
- **B:本轮只评在跑的**(Session 层+治理)—— 工作量小,但记忆评测主体(跨会话留存)缺席,票 11 砍半
- **C:长期层启用在别处已计划,本轮只做评测** —— 需指出那条线在哪

风险提示:启用后 Dream/巩固会引入额外 LLM 调用(写路径成本),票 10 的写成本指标正好覆盖 —— 先插桩后启用,启用即有度量。

## Comments

**Resolution (2026-08-20,用户认可 ❹)** — 选项 A,执行形态:

- **评测启用路径**:harness 直调 `main_agent_memory(archive_enabled=True, core_enabled=True)`(presets 传参;AND 门自动派生 DreamEngineConfig)—— 不新增配置面,不动物理出厂配置
- **DreamEngine 显式驱动**:摄入后 harness 显式 `run()`,不用后台轮询(background.py 形态)—— 探针可确定性控制巩固时机
- **冒烟验收**:一次真实"摄入→巩固→注入"链走通(core 开时 `ensure_long_term_defaults` 播种 + CoreMemoryConsolidator 构建 + Dream 消费 archive → core 注入)
- **生产默认不动**:出厂 pool.yml 无 memory 块 = 全关;WebUI 开关留给用户产品决定。**子代理保持 session-only**(五型探针全走 main-agent 形状,不受影响)
- **顺序**:09/10 插桩先行 → 本票启用冒烟(先插桩后启用,启用即有度量;写路径成本正好被票 10 覆盖)
