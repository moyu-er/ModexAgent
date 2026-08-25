# Wayfinder Map: 评测体系接入 (Eval Integration)

Status: closed
Labels: wayfinder:map
Closed: 2026-08-20 — 全部 14 票决议完毕(8 设计票 + 1 spike 执行票 + 5 记录/裁定票);后续动作见"Next"

## Destination

建成并接入完整的评测体系:Terminal-Bench 官方基准(installed-agent 模式)、经校准的 LLM-as-judge、成本核算,以及**记忆(上下文)能力评测**(遥测插桩、确定性组件指标、探针套件、judge 记忆特化、e2e 哨兵)—— 轨迹、判定、成本、记忆指标、实验对比全部以 OTel/Langfuse 为单一中心收敛,使框架/提示词/模型/记忆机制改动**能够**被这套体系评测与迭代。体系接入完成即到达终点;用体系做优化本身是终点之后的事。

## Notes

- 领域文档:`examples/bot_project/bot/eval/AGENTS.md`、`src/modex_agent/trace/AGENTS.md`、`examples/bot_project/docs/langfuse/langfuse-deployment.md`、`examples/bot_project/evals/README.md`(cassette 四门契约)
- HITL tickets 一律先加载 /deliberate
- 记忆线调研已固化:`research/memory-eval-landscape.md`(基准景观、主流实现工程模式、通用借鉴模式 —— 票 09-14 的共同输入)
- 记忆线约束:长期层启用已裁定(票 13):仅评测 preset 启用 + harness 显式驱动 Dream,生产默认关;子代理保持 session-only
- 既有约束(制图前已定,不需要再开 ticket):
  - Langfuse 为判定/度量/对比的单一中心;重证据文件(patch、官方 harness 日志)留本地 gitignored 目录,以 trace metadata 引用关联
  - 容器内 tracing 首选 OTel collector 路径(现有 collector,127.0.0.1:4318);FILE 后端是已存在的 ABC,若实现依然简单则纳入设计规划
  - Terminal-Bench 为公开基准主锚;SWE-bench 本轮不接入,仅保证"每个基准一个独立 host-half 适配器模块"的扩展位,不做引擎级抽象
  - 真实模型评测走采样制,预算 ~$15/轮;CI 回归零 API 成本
  - judge 采纳"单次 rubric 评审 + temperature=0 双层钉死 + 独立 JUDGE_* 环境变量 + 全量 I/O 审计"形态,但以被 trace 的 agent run 实现,judge I/O 自然进 Langfuse;prompt 版本化与校准门槛为超出该基础形态的增强
- **产出文档只描述模式与决策本身**(installed-agent 模式、rubric judge 等),不引用任何外部来源或临时参照物

## Open tickets

(无 —— 全部关闭)

## Next(地图收官后的动作,非本 map 票)

- **实现排序**:全部设计票的切片已就绪(09/10/11/12/13/02 + 03/04/06/08),待用户启动实现会话;建议序:09+10(插桩与指标,含 CleanupMetricsHook 退役)→ 13 冒烟 → 11+12(探针与判官)→ 02(适配器)→ 14(哨兵,执行)
- 雾区中与实现强相关的两项(FILE 回退已勾销;dataset/探针库冻结策略随 11 实现落)

## Decisions so far

- [Langfuse 成本能力调研](issues/05-langfuse-cost-capability.md) — OSS v4 完全支持自定义模型定价(project 级,OTLP 同路径计价,cache 桶可用);短板是摄入时计价不回溯 + 价格表不在 git → 指向"本地价格表为真源 + 同步推 Langfuse + cost_usd score 注入"的分工(详见 research/langfuse-cost-capability.md)
- [Golden 库清理](issues/07-golden-library-cleanup.md) — 四个 v1 用例全删(弱断言不足以锚定标准);cassette 机制/四门契约保留;CI 回归暂停至手动 dispatch;v2 重建标准以 TODO 落 evals/README.md,重建本身待办(触发:judge 架构落定)
- [成本核算设计](issues/06-cost-accounting-design.md) — 两层价格表(框架内置 + bot 覆盖)为单一真源;逐 turn `cost_usd` score 与 12 指标同批注入,compare 聚合成本列;双源(Models API 同步 + trace 属性)为二期;实现依赖工单 08 的 score 命名契约先落
- [Langfuse 判定契约](issues/08-langfuse-verdict-contract.md) — 契约 v1:12 指标裸名不迁移,新增 `cost_usd`/`judge_*`/`verdict_<benchmark>` 三类;溯源走 score comment JSON-in-string(实测确认 v4 metadata 字段被静默丢弃);基准采样=experiment、instance=dataset item、重证据本地 run_ref;verdict 注入义务落票 02
- [Judge 架构设计](issues/03-judge-architecture.md) — 单次 rubric 评审 + 最小 trace 包裹(judge I/O 自身进 Langfuse);证据层级化上下文;集中 rubric 库(二值判定/正交校验/版本 hash/CANNOT_ASSESS 出口);独立 judge pass CLI;判决按契约 08 落库;隔离调用与 agentic judge 为升级路径
- [Judge 校准门槛](issues/04-judge-calibration-standard.md) — 最小验证协议裁剪:pilot 10 实例先行(κ 崩先修 prompt);门槛 κ≥0.6/0.67、test-retest≥95%、NA<5%、方向偏斜必报;变更即重校;未校准分落库但标 `calibrated: false` 标灰,永不静默用于决策
- [记忆遥测插桩](issues/09-memory-telemetry-spans.md) — 新建 `memory_trace` hook(工厂注册,默认关,roster 增量启用)+ 3 个新 hook point(read/write/consolidate,挂已存活 seam);OTel collector→Langfuse 唯一真源,CleanupMetricsHook 与本地 JSONL 按遗留退役(与票 10 的 metrics.py 改读 span 同批);09 交付 span+计数器,分数归 10/11
- [长期记忆层启用](issues/13-longterm-memory-enablement.md) — 仅评测启用(harness 直调 preset 双开 + 显式驱动 DreamEngine,非后台轮询);生产默认关;子代理 session-only;插桩(09/10)先行、启用冒烟随后
- [记忆探针套件](issues/11-memory-probe-suite.md) — 程序化真值世界(fact_ids 标注)+ 双侧渲染 + 五型题(隔离题双信号:上下文污染确定性检查+答案判官);真实写路径摄入 + 32k 评测窗口 + 显式 Dream + 快照;load(query) 组装 + 裸 LLM 作答(agent 不参与);确定性召回优先,分数注入义务在 harness;125 题冻结进 git,每轮 ≤$5
- [Judge 记忆特化与校准](issues/12-memory-judge-specialization.md) — 证据引用门(归一化 substring 校验,引文不实即降级 UNMET);知识更新题三档判定;独立失败电池 30 例(prompt/模型变更即重跑);答题/判卷模型分离硬规则 + 可审计覆盖开关;校准复用 04
- [确定性组件指标](issues/10-deterministic-memory-metrics.md) — 指标全从 09 span 规约;压缩三轴硬断言锚定自身配置不变量(峰值≤max_token_ratio×窗口/每次严格降/前缀稳定);写成本归因边界:per-turn cost_usd 不含后台记忆成本,run 级 memory_write_cost_usd 汇总;CI 切片零 LLM 零网络(快照重放+不变量断言),gating=冻结基线上新增失败即红,绝对阈值待基线;metrics.py 改读 Langfuse 与 CleanupMetricsHook 退役同批
- [E2E 记忆哨兵与消融臂](issues/14-memory-e2e-sentinel.md) — 自定义 3 任务 mini-chain(任务 1 建立事实,2/3 新会话结构性依赖);消融臂编进 experiment 名(.memory/.nomemory),仅持久记忆跨界;2 臂×3 任务 ≈$3-5/轮;对 02 的硬输入:链内共享记忆 namespace + 按实例隔离;执行依赖 01/02,设计已关闭
- [Harbor 可行性 spike](issues/01-harbor-feasibility-spike.md) — **GO(带网络降级)**:installed-agent 契约/工件回收(/logs/agent 自动回收)/容器→collector→Langfuse 全链实测通过(L2 fake provider + L3 真实模型 reward 1.0,~$0.01);源码 tar(含 modex-graph)3.5MB + venv + .pth 引导可行;降级项均为本机网络(容器内 pypi TLS 阻断→镜像源,github release 下载失败→TB verifier 的 uv 自举需预案,apt 慢→timeout multiplier 或预建镜像);TB 2.1 镜像普查:38/89 为 ubuntu:24.04 无 Python→适配器须自举或限定 python 镜像;Langfuse 丢弃固定历史时间戳 span;详见 research/harbor-spike-findings.md(含票 02 的 8 条实测设计要点)
- [ModexHarborAgent 适配器设计](issues/02-modex-harbor-agent-design.md) — 三档探测式引导(有 python 直装/apt 档/无 python 一期 NO_TEST,uv standalone 为二期);容器内 env 自建 agent 复刻 harness 模式,零宿主转发;tracing collector 直连(`MODEX_OTLP_ENDPOINT`),**FILE 回退砍掉**;产物全落 /logs/agent + result.json 只读回注 verdict(采集脚本义务);归属 bot/eval/harbor/ dev-only 零产品暴露;切片 ⑦ 项含 verifier 网络预案

## Not yet specified

- judge 校准达标后,是否/如何对生产 trace 采样评审(需基准达标后**单独再校准**——生产分布≠基准分布)
- 生产 trace 的记忆行为采样(依赖上一条 + 记忆遥测落地后的分布认识)
- Terminal-Bench 采样之后的规模化策略(更大样本、多机、全量 split)——依赖 spike 与首轮采样结果
- 基准数据集与记忆探针库的版本冻结策略(怎么钉版本、何时允许升级/再生成)
- 容器内 FILE 回退 tracing 是否值得实现(依赖 spike 对 collector 网络可达性的结论)
- golden v2 重建的排期(标准已备:evals/README.md TODO 段;触发条件"judge 架构落定"已满足,启动待用户裁定)

## Out of scope

- **SWE-bench 实际接入** —— 仅保留扩展位(见 Notes);需要时另起 map
- **用体系做一轮真实优化** —— 本 map 终点是体系接入可用;优化是体系建成后的使用行为
- **golden/基准任务库扩容** —— 现有用例质量低,清理后 CI 暂停执行,补库是后续工作(在清理 ticket 中留 TODO 指引)
- **本地 eval 的持久容器隔离**(执行环境 Docker 化)—— 公开轴的隔离由基准 harness 承担;本地轴暂以环境无关任务设计规避
