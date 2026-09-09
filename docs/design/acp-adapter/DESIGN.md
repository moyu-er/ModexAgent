# ACP 单项目接入：收敛设计

> 状态：**基础实现及自动回归已完成，编辑器人工验收未完成**。ACP 实现选择性迁入到共享收敛基线 `189e0312` 之上，不是该基线提交本身的内容。Windows 真实 stdio 使用生产 bot 装配与 scripted provider，覆盖 FILE/SQLite 重启加载、相对路径工具、once 审批、取消后新请求及不支持的命令；未调用真实 LLM 或启动 IDE。完整验证范围见 [PLAN.md](PLAN.md)。
> 更新：2026-09-10；共享基线 `189e0312`，ACP 源码以当前分支为准。
> 唯一设计基线：本文。任务、依赖与验收追踪见 [PLAN.md](PLAN.md)，不得在规划中另定义行为。
> 单项目产品范围：**一个 ACP 进程、一个 IDE 项目 workspace、一个选定 pool、多个会话；不切换 workspace/pool。**

## 1. 产品契约与非目标

### 1.1 IDE 使用方式

IDEA 打开 `D:/projects/my-app`，启动 `modexbot acp --config <bot配置目录>`：

1. initialize 只建立协议连接，不假定操作系统 cwd 是 IDE 项目，不启动 bot_project 的 home pool。
2. 第一个合法 new/load 请求的协议 `cwd` 决定本实例的项目根。
3. 业务侧以该项目根装配 workspace 和选定 pool；new/load 在装配成功前不返回可用会话。
4. 同项目可创建/加载多个会话。后续请求若 cwd 不同，明确 project-mismatch，不修改当前绑定。
5. prompt、approval、cancel 只通过 sessionId 定位，不增加自定义 workspace 参数。
6. 编辑器打开另一项目，使用另一 ACP 实例；ACP 不提供 `/cd`、workspace 列表、workspace/pool 切换。

协议要求在 new/load 携带 cwd 时照常遵守；这是首次绑定/后续校验，不是要求用户额外配置 workspace。内部 InputMessage.workspace、scope 与工具 cwd 仍由已绑定资源填充，不删除框架已有上下文。

### 1.2 本阶段交付范围

- 原生 main 的文本、工具、once 审批、完整取消、子任务结算、连接关闭。
- 项目内多会话隔离、同 session busy 防护、同项目原生会话加载与真实历史回放。
- 真实能力声明与 unsupported 输入处理；Windows stdio 和 IDE 验收。
- 默认模型来自既有配置；基础只提供 default 模式。不实现新模型下拉、plan/auto、always、图片、diff、usage、elicitation。

后续功能列入 PLAN 的延期清单，不作为隐含前置，也不以空接口声称支持。external main 沿既有 execution strategy 接缝，但只有经过单独验收的能力才能声明；基础可明确拒绝未验收的 external pool，不能按 provider 名称在 ACP 内实现替代运行路径。

### 1.3 原有功能保护线

- resident bot 的 home、配置解析、动态 workspace、`/cd`、`/pool`、IM/WebUI 会话身份与历史不变。
- IM/WebUI 的 QUEUE/STEER/INTERRUPT、`/stop`，graph pause/resume、eval 完成合同不改变。
- 不强制旧会话增加 ACP binding，不批量迁移普通会话，不让普通活动会话因 ACP 重启被标 interrupted。
- 不重构全局模型 LRU、metadata 更新体系或跨入口会话配置；本阶段没有新的运行时模型修改。
- 不新增跨进程共享 live pool、文件锁、重试器、completion tracker 或 ACP 数据库。

**尽可能零侵入指行为零变化，不承诺共享代码零改动。** 共享 owner 必要增强必须有原行为特征测试；若需要改变保护线，停止该任务并单独提出范围变更，而非假称机械迁移。

## 2. 当前事实与设计约束

> 本表已在 `189e0312` 上按源码重核。旧行（`AcpSession`、direct pipeline、ApprovalBridge 等）描述的实现已被删除，不再是当前事实。

| 代码锚点（仓库相对路径） | 当前事实 | 设计处理 |
|---|---|---|
| `src/modex_agent/acp/backend.py` | SDK-free ABC seam：`AcpSessionBackend`/`AcpSessionHandle`/`AcpInteraction`（§8 已实现，无 legacy factory） | 稳定合同；能力扩展沿同一 seam |
| `src/modex_agent/acp/server.py::ModexAcpAgent` | 四 RPC；new/load 把协议 cwd 转发 backend，per-session busy gate，cancel 路由（load 取消失败 load RPC），aclose 关闭门+协作 drain | 仅映射协议；项目绑定由 backend 决定 |
| `src/modex_agent/acp/scripted.py` | `ScriptedAcpBackend`（echo/approve/wait，`supports_load=False`）——无 LLM 的 e2e turn 源 | 测试/demotic 后端，同一 seam |
| `examples/bot_project/bot/acp/runtime.py::AcpRuntime` | 生产 backend：首 cwd 绑定项目、唯一 boot task、`BotAssemblyRoots` 装配（workspace_home=IDE root，`enable_dynamic_workspaces=False`）、pool 显式解析固定、native ReAct main 校验、`settle_interrupted_scopes()` 先于会话开放 | §4 状态机与 §3.2 roots 的落地 |
| `examples/bot_project/bot/acp/driver.py::PoolAcpSessionHandle` | request-scoped 执行：`begin_request` → shared prepare → `pool.run_input`；审批经 `ApprovalRequestView`→`PermissionPrompt` 往返，决策经同一 prepare/`submit_input` 提交回**审批源 session**；cancel 走 `tree.cancel_request` + 权限任务 drain | §6/§7 已实现；无 direct pipeline、无推断/缓存 |
| `examples/bot_project/bot/acp/emitter.py::AcpEmitterHub` | 单路由规则：直接注册即 owner，其余沿父链解析到打开的 root；child 工具事件按 source 命名空间、child 审批带 SOURCE 路由，无 owner 即 raise（owner 取消 scope） | §7 输出单写与子审批真实源路由的落地 |
| `examples/bot_project/bot/acp/identity.py` | `AcpSessionOrigin` typed 标记（metadata `acp_origin`）；create 一次写入，validate 拒绝 resident/异 pool/child | §5.1/§5.2 的落地 |
| `examples/bot_project/bot/service/roots.py::BotAssemblyRoots` | 已存在：`config_dir`/`resource_root`/`workspace_home` frozen 值，`resident()` 复现历史路径 | §3.2 的落地；`_project_dir` 仍为业务资源根 |
| `bot/service/core.py::initialize/stop` | 已接 `roots`/`enable_dynamic_workspaces` 装配输入；ACP 路径 init 未 start，close 统一经 `BotService.stop` | §4.3 的落地 |
| `bot/workspace/wiring/stack.py::build_workspace_stack` | 通用 registry+bot 工厂，资源经 `service.roots` 解析 | 复用，未新建 ACP workspace manager |
| `bot/input_pipeline/assembly.py::build_acp_pipeline` | ACP 与 IM/WebUI 同一 stage 编排 skeleton，`prepare`/`handle` 分离 | §7 的落地 |
| `src/modex_agent/multi_agent/session_tree/request_scope.py` | 已存在：RequestScope/reservation/outcome 归属事实（原 tree owner） | §6.2 的落地 |
| `src/modex_agent/multi_agent/pool.py`（`begin_request`/`run_input`/`submit_input`） | 可等待请求与单向输入共用 poller/deliver 路径 | §6.1 的落地 |
| `bot/control/facade.py::history`（`read_pool_session_history`） | 源读取共用，wire 投影独立（`map_history_replay`） | §5.3 的落地 |

已删除（不再是当前事实，不得引用）：`src/modex_agent/acp/session.py::AcpSession`（私有锁/字符串 driver）、`approval_bridge.py`（含 `ApprovalBridge`、批次模型映射、`always` 选项、`PermissionPrompt.turn_id` 字段）、`history_replay.py` 独立模块（回放映射并入 `events_map.map_history_replay`）、ACP direct process_message、None+输出缓冲完成判断、remembered_tools、伪 `ApprovalRequestState`。权限面为 once-only 对（`DEFAULT_PERMISSION_OPTIONS`）；scripted 的审批取消即 drain 权限任务，不产生 denial 事实。

旧文档中 resume、diff、plan/auto、always 的"已支持"不作为事实。当前 SDK 是 Python `agent-client-protocol==0.12.1`（`pyproject.toml` optional extra `acp`，已钉死）。协议整数、规范修订、SDK 和客户端版本分别核对。此前 44+7 测试通过不是本稿运行验收。

根路径的 ARCHITECTURE-MIGRATION-PLAN.md 缺失；已读 gitignored `docs/handoff/ARCHITECTURE-MIGRATION-PLAN.md` 背景副本。本设计只增强现有 owner，不移动 core 或执行 A0–E2；若要跨越此界线须恢复正式基线。

## 3. 所有权与路径模型

### 3.1 所有权

| 关注点 | 框架 | bot | ACP 协议 adapter |
|---|---|---|---|
| workspace/pool | 资源接口、registry、执行机制 | 项目绑定、配置来源、pool 选择、分区 | cwd 转绑定请求 |
| session | SessionInfo/Store、父子关系 | 选定资源内注册与校验 | opaque sessionId |
| 请求 | tree/poller 准入、审批/结果事实、取消结算 | 交互 adapter 装配 | await 与 RPC 响应 |
| 权限 | 分类、事务、snapshot/audit | 原有配置 | once 选择往返 |
| 输入输出 | 通用消息/事件契约 | 输入准备、单写记录、输出装配 | wire 投影 |
| 关闭 | 资源自己的 finalizer | BotService init/stop | 断链停止准入 |

Pool 是框架概念；“当前项目用什么 pool、配置从哪里来”是业务决定。不得新增 AcpWorkspaceManager、AcpPoolRegistry、AcpCompletionTracker、AcpPermissionEngine。

### 3.2 三种根目录必须分开

| 根 | 来源 | 用途 |
|---|---|---|
| 配置根 | 显式 `--config` 的绝对目录 | bot/model/scope/MCP 配置、配置相对路径 |
| 业务资源根 | 现有安装/项目资源解析器 | bundled/project plugins、声明引用的技能与资源 |
| 运行 workspace 根 | 第一次有效 new/load.cwd | 工具 cwd、scope workspace、session/memory/runtime/media/transcript 数据 |

实施在 bot 装配输入中加入 frozen `BotAssemblyRoots`（提议名）或等价 typed 值，集中生成来源；不在各消费点反复 `if acp`。resident 默认值必须与当前路径逐项相等。`_project_dir` 保持业务资源根含义，另用明确 `workspace_home`，不能把所有引用替换成 IDE root。

必须审计的路径消费点：scope boot（声明路径、project_dir、data_dir）、MCP registry、plugin discovery、graph 搜索路径、skills/capabilities 资源、registry DB、home DB、pool routing store、ctx.paths、工具初始 cwd、外部 provider cwd、stop 所持 DB。

ACP scope 配置取显式 config 根；workspace graph/resources 仍按原声明解析规则，不能要求任意 IDE 项目包含 bot 的 `config/scopes/bot.yml` 或 `plugins/`。不自动加载 IDE 目录为 bot 插件，不复制配置进 IDE 项目。共享路径 resolver 只做分离职责，不改变 resident 原优先级。

### 3.3 数据布局与并发实例

运行数据使用当前配置的 `data_dir_name`，根为 IDE workspace；registry/home 等运行 DB 都属于该实例的同一项目数据布局，不残留写 bot_project 的 home DB。

同项目的两个独立进程可能写同一个 data root；当前不具备跨进程互斥保证，**只支持单 writer 实例**，必须在部署说明明确。未增加锁或自动租约；不同项目 data root 独立。不能宣称两个 IDE 窗口同项目并发安全。只读 UI 不自动获得同进程控制权。

## 4. 项目绑定与生命周期状态机

### 4.1 进程绑定状态

`UNBOUND → BINDING(root) → BOUND(root) → CLOSING → CLOSED`

- initialize 可以发生在 UNBOUND，只返回静态协议能力，不创建 BotService home pools。
- 首个合法 new/load：用原路径规范化/校验规则校验绝对 cwd，原子保留 root 后开始唯一 boot task。
- BINDING 时同 root 请求等待同一个 boot task；不同 root 立即 project-mismatch。取消一个请求不能取消其他调用者共享 boot。
- 无效路径在保留前拒绝，不绑定进程。保留后 boot 失败进入 FAILED/CLOSING，调用原 owner 清理后退出，不能在同进程换 root“重试”。
- BOUND 后不同 root 始终拒绝；load ID 不存在不会改变已绑定项目。
- 关闭前还未绑定：不创建资源，直接结束。boot 中 EOF：runtime 取消/等待唯一 boot 的 rollback，再关闭。

路径比较复用 Windows drive/case/symlink 规则；同路径拼写变化不误判。禁止 os.chdir。首次绑定不是 `/cd` 操作，不受 resident “是否允许切换”开关误阻塞；复用路径验证函数，ACP 不改 WorkspaceController 的 resident 行为。

### 4.2 单项目装配

在唯一 boot 中：

1. 构造 roots，加载已有配置/registry。
2. 初始化 BotService，workspace home 为 IDE root；ACP 入口不注册 workspace 切换入口，也不重放 resident 动态 workspace 声明。该装配选择是业务启动合同，不是 provider 特判。
3. 只 materialize 运行 home；pool 按显式 --pool/config pool→单 pool→default 解析并固定。声明依赖的 peer pools 可由原工厂装配，但 ACP 不对外切换到它们。
4. 选定 main/pipeline/capability 均存在才 BOUND。池缺失或 external 能力不足明确失败，不回落 bot_project。
5. 不调用 BotService.start；stdio 自己提供输入，不启动 IM/WebUI 服务循环。

### 4.3 资源关闭

connection closing→拒绝新操作→取消本连接 owned 请求→等待原 tree/poller finalizer→关闭交互/订阅→BotService.stop→资源逆序关闭，DB 最后。

stop 支持 initialize 未 start、partial init 已 rollback，幂等等待同一 finalizer。错误保留，不能中断后续可执行清理。原 BotServiceShutdownIncompleteError 由 owner 报告；entry 不新增 retry/sleep/timeout，最终不可恢复则 stderr+非零退出，不声称清理成功。

## 5. 会话与恢复：不扩大成全局绑定工程

### 5.1 会话地址

实例已持有唯一 workspace 与选定 pool，调用者只传 sessionId。内部资源引用来自 bound runtime；不新增全局 session→workspace map，不启用闲置 session_workspace_map，不引入自描述路径 token。

new 使用服务端新 SessionInfo，protocol sessionId 等于完整 framework ID。仅创建在选定 pool 的 SessionStore 中。原 store/registry 负责身份，原 pool resolver/注册 hook 负责其必需路由，ACP 不另定义双写修复系统。

新 ACP 根会话创建时一次写入 typed immutable origin 标记（现有 metadata 扩展位）：版本、origin=ACP、选定 main/pool。原 tree owner 同时登记该会话的 request-scoped 执行策略。workspace 不重复写入，由 store 的根确定。标记只是验证/恢复策略，不是第二份身份记录；不要混用 SessionBindingStore 的 graph/task binding 或 ExternalSessionMapStore。

创建必须 await register/save 与 tree 策略登记成功后发布；中间失败的未发布记录保持不可执行，load 校验策略不完整即拒绝，由原会话清理 owner 处理，不自动补成普通 session。首次 save 通过已选 pool 的原解析接口定位；禁止缺映射时误写 `main` 目录。若现有注册 hook 时序不满足，在既有 register/装配接口内修正且验证其他调用者，不在 driver 写文件。

### 5.2 加载

new/load 的 cwd 仅用于项目一致性校验；在**当前选定 pool**读取原会话。校验 origin、main/pool、存在性与运行状态。未知/普通 resident/旧未标记 MVP 会话明确 unsupported/not-found，不猜测，不要求全仓会话迁移，不改变其原使用。

底层 get 若跨 pool glob，使用原 store 的精确 pool 绑定/过滤能力；若必须补接口，在该 store owner 提供精确访问，不复制 scanner。不得取第一条冲突记录或回落其他 pool。

同 session 只存在一个可操作 handle；重复 load 已打开会话返回 busy/already-open。本阶段不实现跨入口共同编辑和多个 ACP handle 的写锁共享。不同 sessions 可以并发。

启动/加载时只结算 origin/request-scoped 明确属于已断开的本地 ACP 交互请求；不能因 poller 暂时没有 task 就终止 graph/resident 的工作。恢复过程中不得自动重放旧 pending 工具。已被取消的工作不进入新 prompt。

原 tree.can_dispatch 对 request-scoped 会话要求本实例恢复完成的 live ownership：持久化 active/awaiting 而没有 live owner 时禁止 poller admission。该检查必须在 pool 工厂可能启动 poller之前生效（可由原 tree 装配时加载策略），不能等 load 才阻止已经执行的工具。boot 先完成 scoped 遗留结算再接受会话，普通/graph 的既有 admission 规则不改。

### 5.3 回放

加载进入 REPLAYING，prompt 返回 busy；取消 replay 解除只读发送任务，load 返回明确取消错误而非 PromptResponse。断链关闭，不能报部分成功。

bot 共用现有历史来源读取（native MessageStore、external canonical transcript），ACP 接收 typed facts。不得把 CLI 8 字段有损 HistoryMessage 当完整历史，也不得在框架 ACP 直接引用 SQLite adapter。复用源读取 helper 时原 facade 的顺序/字段/limit 不变。

读稳定快照→按真实顺序 await wire updates→load 成功→IDLE。不执行工具/审批/LLM，不新增 memory/transcript/usage；缺 reasoning、diff、用户消息就不补造。源 ID+turn namespace 构成稳定 tool ID；缺源 ID 的序号仅作回放关联，不写回事实。

## 6. 框架请求结算：共享实现，明确适用域

### 6.1 保留既有机制

- InboxPoller 是唯一 pool executor，原 `_inflight` 负责 single-flight。
- SessionTreeManager.wait_quiesce 是唯一完成等待机制；不新增 Event/tracker 或轮询。
- `submit_input` 单向输入保持原合同；新增 `run_input` 是同一 submit/deliver 路径的可等待形式，不 direct pipeline。
- 无 pool 独立 AgentPipeline 的现有接口仍可用。

**范围限制：** 可等待、可终止的“交互请求作用域”由调用方显式接纳为原 tree owner 的 RequestScope（提议名），不是 `if channel == acp`。ACP 新建的私有会话采用它；ordinary/graph session 不自动转换。scope 不提供第二个 executor/store/signal；只是现有树内当前用户请求的归属事实。

同一个关注点只有一个实现：消息仍由原 bus/track，取消仍由原 poller/finalizer，审批仍由原 resumer/coordinator。普通 submit 和 graph pause 是不同调用合同，保留它们不属于旧实现 fallback。禁止新增一个“ACP tree manager”。

### 6.2 最小持久化事实

在原 tree/node/track/snapshot owner 添加可选 typed request_scope：root、请求 ID、当前状态、最终 root result、待决 approval 关联。envelope/track/task/result 在既有公共发送与回传路径传播作用域。

- 普通记录无 request_scope：继续原语义，不批量标 interrupted，不重新编号所有 resident 请求。
- 有作用域的会话：一次一个用户请求；第二个新 prompt busy，不支持在 ACP 内 QUEUE/STEER/INTERRUPT。审批继续匹配同一 scope，child reply 继承 scope。
- scope 缺失不是兼容回落：是未使用 request-scoped 合同。**一旦目标 session 当前属于 scope，后续因果消息缺失/冲突归属即拒绝，不能当普通消息执行。**
- pending reservation 必须在 deliver 可能挂起前可见；原 send/dedup 事实由 bus owner 明确区分，必要时用 typed accepted/already-present/failed 结果替代混淆 bool，并让调用者委托同一发送实现。
- scope 不重用到下一请求。terminal outcome 在唤醒前冻结，下一请求不能覆盖仍被读取的结果；既有 tree store 持有当前/必要终结事实，不建 completion DB。
- 会话选择 request-scoped 策略后，admission 模式在该会话生命周期内固定；IDLE 时未持有当前 token 的普通 submit 也不能启动它，避免其他入口绕过 scope。策略判断是原 tree 的显式执行合同，不检查 ACP 渠道字符串；普通 session 不受此限制。策略的存储在原 tree 记录，bot origin 只决定是否允许打开/恢复，不是第二份运行状态。
- 原独立用户队列不进入 ACP scoped 会话，因此不需给全部 QUEUE 消息重编号。child/session-tree 公共发送路径仍须保留 scope；只有收到完整 child 终结/track 结算才释放当前 scope，不能只看 root task 返回。
- schema 新字段/表若需要，由既有迁移 owner 管理；file/SQLite round-trip 都验证。旧普通数据不被归入新 scope。

### 6.3 pipeline outcome 与审批

内部统一结果为 Finished(AgentResult)、Suspended(真实 request/snapshot identity)、Handled。原 `process_message` 如需保留 AgentResult|None 合同，只映射同一内部结果，不能保留独立推断分支；pool dispatch 获取精确信息。

对 request-scoped 执行：

1. Suspended 事实先于 on_dispatch_end 完成检查登记；不能先 COMPLETED 后登记审批。
2. 既有 tree 谓词针对请求包含 pending approval，因此 prompt 继续等待。
3. 交互 adapter 发 permission，不持 mutation/admission 锁等待用户，不在 owner 内阻塞整个 poller。
4. 回复使用原 ApprovalDecisionInput，经同一提交/恢复机制；匹配 scope/request 后事务提交。
5. 普通 child 回传在 scoped approval 挂起时保留 pending；poller 选择匹配的 approval continuation，不让队首 child 阻塞回复。不能 ACP direct resumer 旁路。
6. 原 IM next-pending、unrelated-input 和 WebUI 行为不改；精准 scope 校验用于带作用域请求。共用事务算法，不复制审批状态机。

基础只有 allow_once/reject_once；删除 ACP remembered_tools 和合成 ApprovalRequestState。permission 的用户取消相当于拒绝批次；session/cancel 终止整个请求，两者不混用。

### 6.4 正常完成

run_input 在同一 tree owner 等 scope 的 root/child/回传后续 root、pending tracks、审批全部结算，读取最终 root result；child result 不覆盖 root。

Handled 在 notice 已发送后映射 COMPLETED，不重复发文本。busy/ownership-conflict 是 RPC error；Suspended 不越过 prompt 返回。结果持久化/必要事件 flush 后才 success。

graph 的无 scope wait/pause 按原语义；不将 graph-owned paused 树挂接成 ACP 子任务，发生则 ownership-conflict。不改变 graph pause 时的 drain-quiescent 含义。

### 6.5 取消

原 pause_session/resume_session 完全保留。新增 `cancel_request(scope)` 位于原 SessionTreeManager：

1. 原子关闭该 scope admission，持久化 cancelling。
2. 原 poller.cancel_sessions 取消该请求 root/descendants，await 同一 finalizer，不能等待自己。
3. 原 approval owner 终结 pending batch/audit/snapshot，不调用新的 LLM；迟到允许结果 stale。
4. 原 inbox/track/buffer owner 结算该 scope 工作；不删除会话/历史，不波及其他 root/peer。
5. 后到 child/provider 结果按 scope 检查归档、不启动新 turn。
6. cancelled outcome 先于 signal；prompt 返回 cancelled。新 prompt 用新 scope，不 resume 旧工作。

只新增这个请求合同；不将 resident /stop、INTERRUPT 改为同样的产品语义。若共享 cancel primitive 提取，原调用者委托它并保持原处理范围。取消不能保证回滚已发生外部效果；外部 provider 无可靠取消时关闭该 pool 的 ACP 支持，不静默假成功。

重复 cancel/EOF 等同一 cleanup。重启只对明确 request-scoped 的遗留 cancelling/active/awaiting 状态做 interrupted 结算，不重放工具；普通记录与 graph owner 不变。

## 7. bot 输入输出：只提取真正共享逻辑

不把全部 QQ/Telegram/WS 的 sync queue callback 改成 async 返回结果。

在现有输入管线中明确划分 **prepare 与 delivery**：原注册 stage 的解析顺序不变，将 EnqueueStage 中最终消息构造集中为一个 typed prepare 结果，普通 handle 调用 prepare 后仍使用原 sync enqueue callback；ACP 使用同一 prepare 再 await pool.run_input。

- 不是在 ACP 复制 S1–S7 列表：assembly 中只有一份适用 stage 选择规则，prepare/handle 消费同一编排。不同渠道原 stage 子集保持。
- `/continue` 等提前投递路径也统一成为 prepared-message 或 handled-notice 结果，普通入口仍原位置投递；不能遗漏第二个 delivery 点。
- prepare 返回 Prepared(InputMessage) 或 Handled，不把 AgentResult 塞入通用 StageResult；bot 所属 typed 模型。
- user transcript 的 S7 保持唯一 writer，审批不写 user message。driver 不手填 RoutingMeta、不直接调用某个 stage。
- ACP 在 prepare 的可写步骤前，先通过原 tree request owner 保留本请求的 admission；成功才允许 S7 落盘和最终 submit。busy/invalid 输入不能先留下 user transcript。reservation token 由 run_input 的共享 admission/submit 两阶段内部接口承接，不新增 driver 锁/完成追踪；prepare 失败由同一 owner 释放未投递 reservation。记录成功而投递失败应记录真实失败并结算，不能删除用户事实掩盖错误。普通渠道仍保持原 prepare/queue 顺序，不引入新的同步等待。
- ACP 只提供 bound session/content/实际 attachments，workspace/pool 来自固定 runtime；只在 ACP command profile 中拒绝 /cd /pool，resident 命令配置不变。

输出复用现有单写事实→多个 sink；ACP 转换不另写 native memory。notice、错误、审批与文本工具均有路由。main/child 使用真实 parent 关系；同项目不同 session 不串流。抽取共享 recorder 时 WebUI/IM 事件字段、顺序、去重规则不变，测试覆盖。

记录生命周期由 `BotTranscriptEmitter` 独占，`WebBotEmitter` 和 `AcpTurnEmitter` 分别投影同一事实；ACP 不继承 WebSocket 实现。`WorkspaceRoutedTranscriptStore` 声明工作区路由及 partial-buffer 合同，固定目录 store 保持原 `TranscriptStore` 合同。审批任务只登记一次，以来源 session、approval ID、tool-call ID 联合标识，批次算法仍属于原审批 owner。

sink 失败引发本连接取消/关闭，但不能抛弃框架 finalizer。记录成功与 wire 成功分开；断链允许无响应，内部必须结算。

## 8. 最小 ACP 接口与错误

异步 backend 在 `modex_agent.acp`，SDK-free ABC；bot 实现装配，scripted 实现测试。替代旧 factory，不加 legacy fallback。

- backend.open(new/load 请求，含协议 cwd) → session handle/description。
- handle.prompt(typed 内容，interaction) → AgentResult。
- handle.cancel() / handle.close()。
- handle.read_history() → typed source snapshot，仅 load 能力开放时调用。
- backend.close() 释放进程级资源；framework async serve 的 finally 确保调用，bot 内委托 BotService.stop。

不预建 configure/mode/model 的空接口。后续需要再在同 seam 增加实际操作。

| 情况 | 行为 |
|---|---|
| invalid cwd | 绑定前拒绝 |
| 已绑定不同 cwd | project-mismatch，原资源不变 |
| pool/模型/配置不可用 | boot 失败并 cleanup，不发布 session |
| unknown/foreign session | not-found/unsupported，不新建 |
| 同 session 重叠 prompt/load | busy，不隐式排队 |
| 不支持的 image/resource/MCP overrides | 执行前拒绝，不能静默只取 text |
| permission 拒绝 | 原审批 resume，后续结果决定 stop_reason |
| request cancel | cleanup 后 cancelled |
| replay cancel | load 错误，不使用 prompt stop_reason |
| EOF | 所有本连接 scope 结算，再 stop |

initialize 的能力是本次进程**所有允许打开的 pool 实现均可兑现的保守集合**；当前只选一个 pool也要避免 boot 前乐观声明。基础限制为原生已验收策略，load 能力只有整套恢复通过才启用。

## 9. 迁移范围与禁止项

| 模块 | 允许修改 | 必须保持 |
|---|---|---|
| acp framework/bot | 接口/driver 全量替换，删除假能力 | optional import、stdio 纯净 |
| BotService/roots/resources | typed 路径来源、显式运行 home、原 stop 接线 | resident 默认路径与动态 workspace |
| pool/tree/poller | 原 owner request scope、准确 outcome、wait/cancel | 无 scope submit、busy 策略、graph pause/eval |
| pipeline/approval | 同一内部 outcome、精准决策、原事务取消 | 原公开返回、IM/WebUI onramp |
| input pipeline | 同一 prepare/deliver 提取 | 原 stage 顺序、callback sync 合同 |
| emitter/history | 共用事实/源读取 helper | 原 UI/CLI 输出 shape/顺序/limit |
| stores | scope 字段、精确目标 pool 访问（确需时） | 普通 session metadata 与旧数据 |
| model-choice/commands UI | 本阶段不改 | 原默认、LRU、/cd、/pool、/stop |

删除：ACP direct process_message、None+buffer 完成判断、remembered_tools、伪审批请求、固定 plan/auto 列表、首次 prompt 才发现初始化失败、loop teardown 代替 stop。

不删除：独立 pipeline 程序化入口、resident sync enqueue、graph pause/resume、现有路由表、非 ACP 恢复策略。它们不是被本任务替换的错误实现。

## 10. 验收合同

全部编号映射到 PLAN 任务。这里定义结果，PLAN 定义如何执行。

| ID | 场景 | 验收 |
|---|---|---|
| B01 | IDE 项目无 bot config/plugins | 使用指定 bot 配置，工具/存储根为 IDE 项目 |
| B02 | initialize 未收到 cwd | 不构建 bot home pool、不写 bot data |
| B03 | 并发首次同 root/异 root | 唯一 boot；同 root 共享，异 root 拒绝 |
| B04 | Windows 等价路径/无效目录 | 正确规范化；非法不占绑定 |
| B05 | boot 错误/EOF/单请求取消 | 无发布、原 rollback；单请求取消不误杀共享 boot |
| B06 | 已绑定后新 cwd、ACP /cd /pool | 拒绝且原项目不变；resident 命令不变 |
| I01 | 同项目多 session | 事件/审批/history 不串；只需 sessionId |
| I02 | new save 失败/错误 pool resolver | 不返回 ID、不写错 pool |
| I03 | 重启 load、未知/普通/旧会话 | 正确恢复自己会话；其他明确拒绝、不迁移 |
| I04 | load replay 中 prompt/cancel/EOF | busy/取消错误/无假成功；源不写 |
| R01 | 初始 deliver/wait 竞态 | 不提前完成、无 lost reservation |
| R02 | root→child→root follow-up | 唯一 poller 执行、最终 root 结果后完成 |
| R03 | 审批挂起且 child 先回 | 回复不被阻塞、无提前 COMPLETED |
| R04 | 多工具 allow/deny/late reply | 原事务原子性、stale 不续跑 |
| R05 | tool/approval/child 中 cancel | 完整 drain，无 orphan/旧工作重放 |
| R06 | cancel 后新 prompt/late provider result | 新 scope 隔离，不污染新轮 |
| R07 | scoped 崩溃恢复 | 只结算 interrupted，不自动重放工具 |
| R08 | 普通记录/graph paused 重启 | 不套用 ACP 恢复、不改旧工作 |
| R09 | materialize/send/dedup/finalizer 异常 | 真实失败结算，不本地 timeout 掩盖 |
| P01 | prepare/handle/continue/approval | 一份 stage 编排、一次 user record、无漏投递 |
| P02 | notice/tool/main/child 多 sink | 原 writer 单写、UI/CLI shape 不变 |
| P03 | 非文本/虚假 mode/always | 不声明、不静默忽略 |
| S01 | init 未 start、partial rollback 后 stop | 幂等关闭、DB 最后、不启动常驻循环 |
| S02 | 多会话审批中断链/重复 cancel | 同一 finalizer 完成，无挂起 |
| S03 | 不可恢复 shutdown error | 非零+明确错误，不报 clean |
| N01 | resident default roots/动态 workspace | 逐项等价，无 data 路径变化 |
| N02 | IM/WebUI busy/命令/审批 | 原语义与测试保持 |
| N03 | graph/eval/无 pool pipeline | 原 pause/quiesce/public return 保持 |
| N04 | 旧 file/SQLite session | 读取不变，无隐式 ACP 标记/取消 |
| E01 | Windows real stdio+scripted model | new/prompt/approve/cancel/load/EOF 完整 |
| E02 | IDEA 当前项目人工验收 | 不要求额外 workspace 配置；cwd 与工具实际项目一致 |

## 11. 最后审视记录与限制

### 11.1 对上一稿的收敛性修正

- 撤掉同进程多 workspace、全渠道 immutable binding、跨 workspace token/路由修复工程。
- 撤掉强制迁移 IM/WebUI /cd、/pool、/stop、busy modes 与普通会话恢复。
- 模型 preferences/全局 LRU 重构、always 原子持久化、plan/auto 从前置任务移到延期。
- 不全量改 callback 为 async；共享准备与不同投递形式分开，保留原 sync 合同。
- request scope 只表达显式新的等待/终止合同，使用同一 tree/poller；不靠 ACP 专属 executor 保持低侵入。
- 首次 cwd 之前不可初始化 IDE runtime；修正“先 init 再 serve”与项目尚未知之间的冲突。
- 配置/插件根与工作根显式分离，禁止粗暴改 `_project_dir`。

### 11.2 自检路径

已逐条核对：UNBOUND 初始化、首次并发绑定、boot 失败/取消、new 保存、重复 load、prompt→child→审批→继续→完成、取消/迟到消息、新轮、replay、EOF/partial stop，以及非 ACP 路径的默认值/旧记录语义。

主要不变量：先登记 pending/approval 再检查完成；不在锁内等用户；不让 dispatch 等待自己的 drain；原记录无 scope 不改变语义，scoped 因果消息却不能缺 scope；停止只影响本连接 owned 工作；数据/事件先结算后成功响应。

上一轮多项目稿曾有独立审查；**本次修订尝试的 general-purpose/Explore 子代理均因 `Model provider is not configured: builtin:bigmodel` 失败，没有完成新的独立审查。** 本次采用主代理依据源码的手工设计/规划交叉自检，不冒称独立通过。实现阶段仍需独立 code review 与 E01/E02，不以文档自检代替。

### 11.3 实施门禁

- 若通用 roots 分离无法保持 resident 默认解析，阻塞 T02，不另开 ACP 资源工厂。
- 若 request scope 需要修改普通 busy/recovery 产品语义，阻塞 T04–T06，重新裁剪而非静默扩大。
- 若目标 SDK/IDE 不兑现假设中的 cwd/load/disconnect，T01 更新 wire adapter 契约；不从 process cwd 猜项目。
- 同 data root 多进程支持仍未提供；文档明确部署限制，不能在验收中声称解决。
- 验收条目是实施门禁：E01 的 scripted provider3 stdio 场景（prompt write 相对 cwd / allow / reject / cancel-next / restart-load）已通过；IDE 人工验收（E02）、真实 LLM、全部 N 项回归与最终门禁**未完成**，不得声称全部验收通过。

## 12. 参考

- [ADR-0049](../../adr/0049-acp-agent-server-surface.md)：保留独立 stdio 与 optional SDK；实施时合并单项目与执行入口细化。
- [ADR-0011](../../adr/0011-approval-batch-atomicity-and-channel-divergence.md)、[ADR-0012](../../adr/0012-input-pipeline-claim-consume-and-unified-approval-decision.md)：审批/输入原合同。
- [ADR-0015](../../adr/0015-unified-inbox-driven-agent-messaging.md)、[ADR-0042](../../adr/0042-scope-declaration-tree.md)：共享 inbox 与 scope 装配。
- [框架词汇](../../../CONTEXT.md)、[bot 词汇](../../../examples/bot_project/CONTEXT.md)。
- 本地 `../../../references/`（仓库外，`F:\tool\pythonProject\references`）下的 `kimi-code/packages/acp-server`、`hermes-agent/acp_adapter`、`opencode` 仅作协议参考，不能决定本项目所有权。SDK 版本已钉死 `0.12.1`（T01 已落地）。
