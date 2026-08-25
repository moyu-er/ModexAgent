# Golden 库清理

Status: closed
Labels: wayfinder:task
Resolved: 2026-08-18 — 全删四用例(超出原"择优"设想,经讨论裁定);机制保留,CI 暂停至手动,TODO 标准落 README

## Question

现有 golden cassette 用例是随手写的、质量低。执行清理(不扩容):

1. 逐个审计 `examples/bot_project/evals/golden/` 现有用例:有真实断言价值且稳定的保留,低质量的删除(删除是本 ticket 的授权动作)
2. CI 暂不执行 golden 回放:`tests_ext/regression/` 的 golden 回归套件从 CI 中摘除(保留代码与四门契约本身,只是不跑)
3. 在 `evals/README.md` 与删除处留 TODO 注释:后续按正式基准任务库标准补建(设计参照本 map 的 judge/基准体系,不纳入本轮)
4. 保留项与删除项列成清单,记入 resolution

这是解锁性的清理动作,不是补库 —— 补库在 map 的 Out of scope。

## Comments

**Resolution (2026-08-18)**: 经讨论扩大处置范围 —— **四个用例全部删除**(裁定理由:弱套件锚定弱标准,部分保留会误导 v2 重建;参考实现亦无 cassette/golden 机制,仅有确定性 fake provider,断言层次设计才是可借鉴点)。执行内容:

1. `evals/golden/` 四用例全删(cassette 机制/四门契约/harness 全部保留不动)
2. `eval-regression.yml` 触发改为仅 `workflow_dispatch`(手动可跑);收集守卫阈值 5→1(双跑身份测试仍无条件收集,导入错误仍会触发守卫);record job 的硬编码用例名改为目录动态发现(空套件时干净退出)
3. `test_golden_replay.py`:空目录守卫(`GOLDEN_ROOT.is_dir()`)+ 双跑测试空套件 skip(原引用 `file-multi-turn` 的 `next()` 会炸)
4. `evals/README.md` 顶部状态横幅 + 新增 "Golden v2 (TODO)" 段:断言分层(执行验证优先/禁零断言)、构成指引(执行验证修复/多轮状态流水线/只读纪律/治理压缩敏感长轨迹 —— v1 全缺,导致二次破坏检查不可用)、环境无关+平台钉死、冻结纪律、flywheel 记录、rubric 维度待 ticket 03
5. `DECISIONS.md` 记录全删决策(含观测依据与恢复触发条件)

本地验证:`pytest examples/bot_project/tests_ext -q` → 2 skipped,零失败;workflow YAML 解析通过。v2 重建留作待办(用户裁定"先不做"),触发条件 = ticket 03 judge 架构落定。

**搁置注记 (2026-08-20)**: 触发条件(ticket 03 judge 架构落定)已满足;所有者裁定 v2 重建维持搁置,排期未定;撰写标准保留 `evals/README.md` 原样,CI dispatch 维持暂停。

