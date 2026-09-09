# ACP 单项目接入：任务实施规划

> 状态：**基础实现与自动回归已完成，IDE 人工验收仍开放**（2026-09-10）。共享基线为 `189e0312`；本次 ACP 迁入与整改建立在其上。原任务条目保留设计与验收定义，当前通过范围以以下验证表为准，不按文件存在与否推断完成。唯一行为规范见 [DESIGN.md](DESIGN.md)。
> 用户已确认：一个进程绑定一个 IDE 项目，不提供 workspace 切换。目标为共享机制收敛、原有功能行为尽量零变化。
> 本文件只定义工作顺序、修改范围、依赖、测试、删除与门禁，不另定义协议或产品语义。

## 当前验证证据

所有测试由主代理串行运行，SDK 为 Python `agent-client-protocol==0.12.1`。

| 检查 | 结果 |
| --- | --- |
| 框架默认全量 `python -m pytest tests/ -q --tb=short --disable-warnings` | 10,981 passed；121 skipped；140 deselected |
| bot 默认全量 `python -m pytest examples/bot_project/tests/ -q --tb=short --disable-warnings` | 2,566 passed；21 skipped；29 deselected |
| 生产装配真实 stdio | 五项通过：FILE/SQLite 两种重启加载、审批中取消后新请求、拒绝无工具副作用、IM 控制命令拒绝；使用 scripted provider 工厂，不绕过 pool/poller/审批/历史 |
| 静态检查 | 本轮 Python 文件 Ruff；16 个 ACP/共享 emitter/shell 源文件 scoped mypy 通过，不代表全仓静态检查清零 |

协议、bot、共享输出和生命周期经过独立只读复审；确认问题已补回归修复。E02（真实编辑器）、真实模型、真实 external provider 能力未验收；跳过与 deselect 不作为通过。缺少 optional SDK 时 ACP 测试明确跳过。框架和 bot 测试分开运行，避免两项目 `tests` 包命名冲突。

本轮收敛删除了旧 direct driver/session wrapper、ApprovalBridge 批次/always 模型和 WebUI segment 自由函数投递路径；共用 pool/tree 请求合同、单次审批交互、`BotTranscriptEmitter` 记录生命周期、workspace transcript 释放路径和既有外部进程 lifecycle。未新增 completion tracker、权限引擎或 ACP 数据库。

## 1. 总交付与完成定义

最终必须同时交付：

- IDEA 项目 cwd 成为 agent 的工作根，无额外 workspace 参数；配置仍来自指定 bot config。
- 原生多会话文本/工具/once 审批/子任务结算/取消/加载回放/EOF 清理。
- 没有 ACP direct pipeline、独立完成 tracker、权限缓存或 workspace 路由数据库。
- resident roots、动态工作区、IM/WebUI 命令与 busy、graph/eval、普通旧会话行为的回归证据。
- 目标 SDK 固定版本、Windows stdio 自动测试、真实 IDEA 验收记录、同步的 living ADR/用户用法。

“文件已修改”“单测绿”“schema 存在”均不等于完成。任务需要实现+迁移旧路径+接口测试+原行为回归+文档事实更新。不能 skip/xfail/delete 测试换绿；遇到既有失败先诊断，修复若扩大范围需记录，不偷偷纳入本需求。

## 2. 范围预算

### 2.1 必要共享代码范围

- bot service 的路径输入/资源接线，不替换整套 BotService。
- 原 pool/tree/poller 的显式 request-scoped await/cancel 合同。
- pipeline outcome/approval coordinator 的真实事实暴露与终结。
- bot input prepare、emitter/history 真实公共部分。
- 原存储 owner 的 scope round-trip 与精确 pool 访问（确有需要时）。

### 2.2 不在本计划修改的产品功能

IM/WebUI `/cd`、`/pool`、`/stop`、QUEUE/STEER/INTERRUPT；动态 workspace UX；全局模型目录/LRU；旧会话迁移；graph 暂停恢复；peer 拓扑；always/plan/auto/多模态/客户端 MCP 注入。

如果必要实现要求改变这些行为，任务标 BLOCKED，写出具体证据与最小替代方案，不通过新增 `if acp/provider` 特判绕过共享 owner。

## 3. 依赖图与集成顺序

```text
T00 原行为基线
 └─T01 协议/客户端契约
    ├─T02 roots 与单项目资源装配 ─T03 项目绑定/会话入口 ─┐
    ├─T04 共享 outcome 与 scope 存储 ─T05 await/审批 ─T06 cancel ─┤
    └─T07 输入准备/输出事实 ────────────────────────────┤
                                                       T08 ACP 接线替换
                                                       ├─T09 原生 load/replay
                                                       └─T10 退出与失败收尾
                                                            └─T11 验收/文档/删除门禁
```

T03 可先以 backend 接口测试实现，但 production cutover 只能在 T05/T06/T07 就绪后于 T08 原子替换。T10 的 finalizer 设计从 T02 起实现，不等最后才加 cleanup；T10 是整体验证与补漏。

并行建议：T02–T03 与 T04–T06 可以分工；T07 先确定不冲突的 helper。共享 `pool.py`/`inbox_poller.py`/`manager.py`/envelope/schema 由同一负责人顺序改；T08 是唯一接线集成点。禁止两个任务分别实现 completion/approval emitter。

## 4. 任务卡

所有任务初始状态：TODO。路径是实施定位范围，不是授权清单以外的全仓改造。

### T00 — 固定原有行为与测试入口

**依赖：**无。**层：**测试/文档。

- 记录 HEAD、working tree、环境 Python/SDK、原 ACP 测试结果；保留用户未提交文件。
- 读规则/相关 ADR；找出 resident roots、input stage 顺序、busy、graph/eval、approval、旧 file/SQLite 的真实测试入口。
- 新增必要的 characterization 测试：默认路径逐项相等、普通记录无 scope 行为、public pipeline 返回不变。
- 将确切命令与原失败记录写入本文件 §7，不能把以前的 44+7 当本次证据。

**不做：**升级包、调整功能以迎合测试。
**验收：**N01–N04 基线可执行；失败已归因。
**产出：**真实测试索引与 baseline 证据。

### T01 — 锁定 ACP/IDE 契约

**依赖：**T00。**层：**ACP wire。

**定位：**`pyproject.toml`、`src/modex_agent/acp/server.py/entry.py/types.py`、`tests/acp/`。

- 核对目标官方规范、Python SDK release notes、IDE 插件版本与实际 new/load cwd、cancel、permission、disconnect 行为。
- 区分 protocol integer/spec tag/SDK semver；选择精确版本，保留 optional extra 与 lazy import。
- 增加契约测试：initialize 不创建 runtime；cwd 必须来自协议，空 mcpServers 合法；unsupported blocks/config/options 明确错误。
- 基础 capability default/once/text；load 只在 T09/T11 完整验收后公开，不能因 SDK 默认值误声明。
- 若版本升级，更新现有映射一次，不保留双 SDK 分支；不在未确认依赖前改变 lockfile。

**验收：**B02、P03；明确目标版本和 IDE 记录。
**删除：**虚假 plan/auto/always 声明；不支持内容的 silent drop。

### T02 — 拆清配置/资源/运行根，复用业务装配

**依赖：**T01。**层：**bot。

**定位：**`bot/service/core.py`、`bot/workspace/wiring/stack.py`、`resources.py`、相关 builders、scope declaration boot/路径解析。

- 引入单一 typed roots 装配值；resident 默认解析完全等价。
- 分清 scope/MCP/plugins/skills/graphs 来源与 IDE ctx.target/data；显式 config 生效，不要求 IDE 项目有 bot 配置。
- registry/home DB、pool routing、stores 都绑定运行 home；修正 home persistence 是否复用的比较对象，不再与资源根混淆。
- ACP 单项目启动不注册动态 workspace 切换入口、不 eager 构建 bot home pool；通过业务装配输入表示，不写 provider/channel 特判。
- 原 ScopeRegistry/资源 factory/BotService.stop 继续唯一 owner。

**验收：**B01、B02、N01、S01；临时 IDE 项目不含任何 bot 配置时可装配。
**删除：**本次引入的重复 path 计算；不移除 resident 动态 workspace。
**阻塞：**若默认 roots 对照不等价，先修源解析，不能另造 AcpResourceFactory。

### T03 — 单项目绑定状态机与会话存在性

**依赖：**T02。**层：**bot ACP/框架 seam。

**定位：**`bot/acp/runtime.py`、`driver.py`、`src/modex_agent/acp/server.py`、原 session store/registry 访问接口。（旧 `src/modex_agent/acp/session.py` 已删除；会话 gate 由 server 的 per-session busy + backend handles 承担。）

- UNBOUND/BINDING/BOUND/FAILED/CLOSING/CLOSED；first cwd bind；同 root 共享 boot，不同 root 拒绝。
- bootstrap 失败和 EOF 归一个 owned task/finalizer；取消单个 open await 不取消共享 boot。
- pool 一次选择并固定；服务端生成 session，await 保存后发布；只在选定 pool 访问。
- 新 ACP origin 标记在既有 SessionInfo 创建时一次写入，执行策略登记在原 tree owner；二者完成才发布，不完整记录拒绝 load。T04 未就绪时只做接口测试，不能声称生产创建完成。不修改普通 IM/WebUI 注册合同。
- backend/handle 最小异步接口与 scripted adapter；production 切换留 T08。

**验收：**B03–B06、I01–I03、I02 保存失败。
**删除：**同步 factory 强迫 first prompt 才 boot；不保留新旧 production 入口同时启用。

### T04 — 共享 outcome、请求归属与 schema

**依赖：**T01。**层：**框架。

**定位：**`multi_agent/session_tree/models.py/manager.py`、原 store adapters/migrations、`envelope.py`、`pipeline/turn_runner.py`、`runtime` snapshot。

- 内部 Finished/Suspended/Handled 事实，原独立 public pipeline 结果由该事实映射。
- 既有 tree owner 管理可选 RequestScope；初始 reservation、状态、结果、approval 标识存入既有存储所属模块。
- 公共 task/回传/envelope 序列化传播 scope；scope 目标缺归属不能降为普通执行。
- 无 scope 普通记录的解码/执行/恢复保持不变；不为原 busy 模式重新定义用户轮次。
- send/dedup 若需 typed 结果，在原 bus owner 收敛，旧 API 的布尔合同若保留仅投影同一事实，不留第二份发送实现。
- file/SQLite schema migration 有旧记录 round-trip 与中途失败测试。

**验收：**R01、R09、N02–N04；原普通消息 round-trip 不变。
**删除：**重复 outcome 推断、隐式把无归属消息分配到当前 scoped 请求。

### T05 — pool 可等待请求与审批闭合

**依赖：**T04。**层：**框架。

**定位：**`multi_agent/pool.py`、`inbox_poller.py`、`session_tree/manager.py`、pipeline/approval resumer/coordinator。

- run_input 与 submit_input 共用 envelope/deliver/poller；不 direct pipeline。
- 原 wait_quiesce 内目标 scope 的 pending/运行/审批/最终结果闭合；不存在另一套 Event 等待。
- 原 owner 的 admission/submit 两阶段接口允许 ACP 在 prepare/S7 前保留 token；prepare 失败释放 reservation，busy 输入不落用户 transcript。scoped 会话的 IDLE 也拒绝无 token 普通 submit，禁止旁路起执行。
- root 多次 follow-up 的最终结果、Handled/失败映射，terminal 快照在信号前固定。
- scoped Suspended 登记先于 dispatch end；匹配审批在同 poller admission 继续，队首 child 不阻塞。
- 普通 IM/WebUI next-pending/unrelated input、graph drain-quiescent 保持原合同。

**验收：**R01–R04、N02/N03。
**删除：**None+输出缓冲猜测；ACP 审批 direct resumer 旁路。

### T06 — 请求取消、迟到消息与恢复

**依赖：**T05。**层：**框架。

**定位：**同 T05，以及 inbox/track 清理、approval snapshot 终结、provider cancellation 公共 seam。

- cancel_request：关 scope admission→原 poller cancel/drain→原审批终结→scope 消息结算→terminal outcome→signal。
- 源头封闭 raced allow；迟到 child/provider result 不重启新轮；取消不永久禁用 session。
- 重复 cancel 同一 finalizer；不在 dispatch 内等待自身。
- 原 can_dispatch 在 poller 启动前即对 scoped 记录实施 live-owner gate；遗留 active/awaiting 无 owner 不准执行，先恢复结算再开放，不能 load 时才挡住旧工具。
- 仅对明确 request-scoped 的遗留状态结算 interrupted；graph/resident 旧记录不受影响。
- 无可靠 cancel 的外部策略不开放 ACP；不加超时假结束。

**验收：**R05–R09、S02、N02–N04。
**删除：**ACP root-only task.cancel 作为完成合同；cancel 后 resume 旧工作。

### T07 — 输入 prepare 与输出单写

**依赖：**T01；接口接 T05。**层：**bot。

**定位：**`bot/input_pipeline/assembly.py/context.py/stages/enqueue.py/stages/commands.py`、原 emitter/transcript 装配、`bot/acp/emitter.py`。

- 将唯一 stage 编排分成 prepare 结果与投递动作；普通 handle 仍使用原 sync callback。
- /continue 提前投递统一归 prepared outcome，不漏掉；审批不写普通 user transcript。
- ACP profile 只拒绝 workspace/pool 切换；resident 命令原样。
- 提取原单写 recorder/事实投影给 ACP sink，notice/error/approval/root/child 不丢；不复制 writer。
- 任何新 helper 通过删除测试；不能仅为 ACP 抄一份 stage 列表。

**验收：**P01/P02、N02；原 queue callback 调用次数/顺序与之前相等。
**删除：**ACP 手工 InputMessage/伪 RoutingMeta、重复 event→transcript 写入。

### T08 — ACP production 接线原子替换

**依赖：**T03、T05、T06、T07。**层：**ACP 两层。

**定位：**`src/modex_agent/acp/`、`bot/acp/`、`modexbot/cli.py`、两套 ACP 测试。

- prompt→同 prepare→pool.run_input；真实 approval view→permission→原决策 submit。
- same-session busy、不同 sessions 并发；cancel 持有 scope 引用，不能取消后一轮。
- framework 不 import bot，bot 不 import acp.schema；SDK 只在协议/映射模块。
- 统一 root/child wire 关联；只发真实支持的信息。
- scripted adapter 同 seam，保留无真实 LLM 的 stdio 测试。

**验收：**I01、R01–R06、P01–P03、E01 文本/审批/取消部分。
**删除（同次切换）：**旧 AcpTurnDriver factory、direct process_message、approval buffer 完成推断、remembered_tools、伪 ApprovalRequestState、静态 plan/auto。禁止 deprecation alias/fallback。

### T09 — 项目内 native load/replay

**依赖：**T08。**层：**bot history/ACP mapper。

**定位：**原 `bot/control` history 源读取、SessionStore/MessageStore 接口、ACP history_replay/new-load。

- 仅当前项目/选定 pool/合法 ACP origin；未知/旧/foreign session 明确拒绝。
- scoped 遗留执行结算后 load；不自动执行 snapshot 中工具。
- 从共享 typed 来源读稳定快照；CLI 显示投影不做输入。
- replay gate 与重复 handle 防护；cancel-load 错误/EOF；无源写入。
- capability load 只在真实 stdio restart 用例通过后开放。

**验收：**I03/I04、E01 restart/load；原 history facade shape/limit 不变。
**删除：**设计中“仅生成稳定 ID 就能恢复”的假定；不加旧 token 双读。

### T10 — 进程退出与失败全链测试

**依赖：**T08；load 路径需 T09。**层：**bot runtime/框架 entry。

**定位：**ACP async serve/finally、BotService.stop、原资源 finalizers。

- UNBOUND EOF、BINDING EOF、BOUND 多会话审批 EOF、重复信号逐一测试。
- 单一 boot rollback 和 service stop，init 未 start 不启动常驻循环。
- partial workspace/provider/MCP/DB 失败故障注入；保留 cleanup 原异常。
- 不可恢复 shutdown-incomplete 非零退出；entry 无 retry/sleep/timeout 包装。

**验收：**B05、S01–S03、E01 EOF。
**删除：**未 await cleanup、只靠 asyncio.run teardown。

### T11 — 回归、真实 IDE、文档收口

**依赖：**T09、T10。

- 执行全验收编号与 N01–N04 对照；记录命令、结果、平台、SDK/IDE 版本。
- Windows real stdio scripted provider 全链；真实 IDEA 项目不含 bot 文件，确认文件读写/终端 cwd/数据位置来自 IDE 项目。
- 将实际支持能力与限制写入用户用法，说明 config 与 IDE root 分离、无 workspace switch、单 data root 单 writer。
- 原地更新 ADR-0049（及被实际细化的其他 living ADR），不新建平行 ADR。更新相关 AGENTS 库存需遵守写作规则。
- 搜索并删除本任务遗留 direct driver/假模式/权限缓存/旧 factory；不删除独立 pipeline 或原渠道入口。
- 独立 code review。若工具不可用，明确人工 review 证据，不能写已独立通过。

**验收：**E01/E02、所有 N 项与最终门禁。
**完成：**仅在对应功能与真实客户端证据齐全时把文档状态改已实现。

## 5. 验收追踪矩阵

| 验收 ID | 负责任务 | 必需回归/负例 |
|---|---|---|
| B01/B02 | T01/T02 | resident 路径不变、无 bot home 副作用 |
| B03/B04/B06 | T03 | 同/异 cwd 并发、Windows 等价路径 |
| B05 | T03/T10 | 单 open cancel vs shared boot、EOF |
| I01/I02 | T03/T08 | 不同 session 隔离、保存失败 |
| I03/I04 | T09 | foreign/旧记录、replay 取消 |
| R01/R09 | T04/T05/T06 | reservation、dedup、worker failure |
| R02/R03/R04 | T05/T08 | child follow-up、审批队首、多工具 deny |
| R05/R06/R07/R08 | T06 | late reply、重启、普通/graph 旧记录 |
| P01/P02 | T07/T08 | 原 stages/queue/recorder shape |
| P03 | T01/T08 | 未声明能力、非法 block |
| S01/S02/S03 | T02/T06/T10 | init 未 start、partial stop、非零错误 |
| N01 | T00/T02/T11 | 所有默认 roots 和动态 workspace |
| N02 | T00/T04–T08/T11 | busy、IM/WebUI 命令/审批 |
| N03 | T00/T04–T06/T11 | graph/eval/独立 pipeline |
| N04 | T00/T04/T06/T11 | file/SQLite 旧记录不归 ACP |
| E01 | T08–T11 | Windows stdio 全链含 restart |
| E02 | T11 | 真 IDEA 当前项目 |

## 6. 变更、合并与失败处置

- 不承诺无文件修改；承诺每个共享修改能指向 DESIGN 中必需机制且有非 ACP 特征回归。
- T04/T05/T06 共享状态转换由一人集成，不能分支分别实现取消/审批 tracker。
- schema 扩展先于消费者；新增可选事实不改变普通记录。不能把无 scope 旧数据默认为 ACP。必要 migration 有 dry-run/备份说明，不对用户真实数据试验。
- 工作中间可以有未启用新接口，但不能在最终 production 中保留并行 driver。T08 是一次明确 cutover。
- 若某项失败，保持该能力关闭并如实报告；不将未完成 cancel/load 伪装为支持。不能静默退回旧 direct driver。
- 每个任务优先接口测试红→实现→回归绿；不真实调用付费模型来跑单元测试。live LLM 与真实 IDE 单独受控验收。
- 未请求 commit/push，本规划不授权提交/发布或依赖升级；实施请求后按相应授权执行。

## 7. 验证命令与证据表

基础已知命令（实施时必须新跑，不引用历史结果）：

```bash
.venv/Scripts/python.exe -m pytest tests/acp/ -q
.venv/Scripts/python.exe -m pytest examples/bot_project/tests/acp/ -q
.venv/Scripts/python.exe -m pytest tests/architecture/ -q
git diff --check
```

框架与 bot 两个 tests 包分开跑。T00 通过目录/测试收集填入 tree/pipeline/approval/workspace/input/model/graph/eval 的真实路径，不猜不存在的 pytest 目录。全仓运行适用时在最后单独执行，测试失败不得以“既有”名义略过。

| 阶段 | 命令/客户端 | 结果 | 状态 |
|---|---|---|---|
| T00 原行为 | 待实施时记录真实命令 | 未运行 | TODO |
| T04–T10 契约/集成 | 待各任务填入 | 未运行 | TODO |
| T11 框架/bot 回归 | 分别执行实际套件 | 未运行 | TODO |
| E01 | Windows SDK stdio 子进程（`examples/bot_project/tests/acp/test_stdio_runtime.py`，scripted provider3） | prompt write 相对 cwd / allow / reject / cancel-next / restart-load 已通过；其余 E01 场景与全量重跑待最终回归 | 部分（不得据此声明 E01 完成） |
| E02 | IDEA/插件版本/项目路径证据 | 未运行 | TODO |

文档本轮检查另记 §9，不混入功能测试通过率。

## 8. 延期工作（不参与上述 DAG）

- session model/config：复用 bot 目录，先确定真实执行快照合同；不先写空 configure 方法。
- always：approval owner 安全匹配+原事务原子记忆后才声明，禁止工具名缓存。
- plan/auto：业务策略+实际约束，不用 PARALLEL/guard-only 假装只读。
- 图片/资源、diff、usage、elicitation：分别沿已有媒体/工具/用量事实，不扩大基础范围。
- external 完整权限/恢复/模型；客户端 MCP/反向文件终端；同 data root 多进程；跨入口同时写同一会话。
- 同 ACP 进程多 workspace 不在路线内；需求变化才另行审议。

## 9. 最后设计/规划交叉自检

| 检查 | 结果/落实 |
|---|---|
| workspace 是否来自 IDE 而非 bot | DESIGN §1/§3/§4，T02/T03，B01–B06 |
| 首次 cwd 之前是否误 init | UNBOUND 只协议，T01/T03 |
| config/resources 是否被 IDE root 覆盖 | roots 用途逐项分离，T02+N01 |
| 是否侵入 resident /cd/busy/模型/旧数据 | DESIGN §1.3/§9，T00+N01–N04；不符即阻塞 |
| 为低侵入是否保留 ACP 第二 executor | 不允许，T05/T08 同 poller cutover |
| 所有等待是否共用 tree owner | RequestScope 是归属不是 tracker；T04–T06 |
| approval 是否堵住自己的 continuation | 同 poller scoped admission，R03 |
| cancel/恢复是否影响普通会话 | 明确 scope/origin，R07/R08/N04 |
| 准备阶段是否复制输入管线 | 同 assembly prepare/handle，T07/P01 |
| 任务是否都有依赖、定位、验收、删除 | T00–T11 完整任务卡 |
| 验收是否都有任务承接 | §5 与 DESIGN §10 一一覆盖 |
| 延期是否被写成假能力 | §8 与 T01/T08 不声明规则 |
| 审查是否如实 | 本轮子代理配置失败，主代理源码自检；未声称独立通过 |

最终文档验证：检查本文件与 DESIGN 的相对链接、任务/验收编号、DAG 引用、无过期多工作区/全渠道绑定要求、git diff 范围。功能验收均保持 TODO。

本轮手工复核补充：busy 输入在 S7 写记录前由同一 request owner 保留 admission；scoped 会话 IDLE 仍拒绝无 token 输入；new 的身份与执行策略均落地才发布；重启的 live-owner gate 在 poller 启动前生效。这些要求已分别加入 T03/T05/T06，不是留待实现自行选择的空白。

文档自动检查记录（2026-09-08）：12 个唯一任务、31 个验收场景均有任务引用，9 个相对链接有效，代码围栏配对，git diff --check 无空白错误。此记录只证明文档结构/追踪，不代表实现测试通过。

原地校准记录（2026-09-09，HEAD `189e0312`）：§2 的 DESIGN 当前事实表按源码重核（ApprovalBridge/session.py/批次模型/always/turn_id 已删除；backend/handle/interaction、真实源审批路由、runtime boot 失败统一 close、SDK 0.12.1 为当前事实）；T03 定位移除已删除的 `session.py`；E01 行记录 scripted provider3 stdio 已通过场景。**这只是文档与源码的一致性校准，不是验收声明**——E02/真实 LLM/N01–N04/最终回归仍未运行，任务不因代码存在而打勾。
