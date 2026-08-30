# ModexHarborAgent 适配器设计

Status: closed
Labels: wayfinder:grilling
Resolved: 2026-08-20 — 五点推荐方案用户全部认可;输入 = spike 实测(research/harbor-spike-findings.md)+ 票 14 硬约束 + scope-converge 装配事实

## Question

设计我们框架的 installed-agent 适配器(基于 spike 事实):

- 引导序列:venv 策略、源码上传、依赖安装(最小闭包 vs slim extra)、`.pth` 方式,install 超时预算
- agent 构造:容器内如何从环境变量构建 ReActAgent(复用 eval harness 的 `_eval_observability` env 驱动模式),terminal/bash 工具映射、max_turns/超时
- **容器内 tracing**:OTel sender 指向宿主 collector(首选);FILE 回退是否实现(collector 路径已有 ABC,FILE 若仍简单则纳入规划 —— 见 map Notes)
- 产物契约:instruction/summary/trajectory 落 `/logs/agent`,console tee;Harbor 的 result.json(官方 verifier 判决)为只读真值,如何引用进 Langfuse(与"Langfuse 判定契约"ticket 衔接)
- 边界:基准 harness 掌数据集/环境/verifier;我们只掌 agent 行为。不做宿主侧逐命令转发。

Blocked-by: Harbor 可行性 spike

## Comments

**Resolution (2026-08-20,用户全部认可)**

**❶ 引导(三档探测式,单一代码路径零特判)**:`install()` 探测 `command -v python3` → 有(python:*-slim 类,41/89)直接走 venv + 源码 tar + pip 镜像源闭包 + `.pth`;无但 apt 可用 → 先 `apt install python3-venv` 再同上;无且 apt 不可用(30/89)→ 一期**跳过并标 `install_skipped: no_python_runtime`,任务记 NO_TEST 不进聚合**(uv standalone 离线自举为二期升级路径)。依赖闭包 = spike Q4 实测(核心 14 + llm + terminal + observability);pip `--index-url` 参数化(env `MODEX_PIP_INDEX`,默认镜像源,宿主/容器一致)。超时:install 600s / agent 运行 1800s(harbor trial 参数)。**票 14 约束落位**:`MODEX_MEMORY_NS`(experiment 臂名注入)挂记忆工作区到容器内固定路径;nomemory 臂 = 同路径逐实例隔离子目录 + 期终清理。

**❷ Agent 构造(容器内 env 自建,零宿主转发)**:容器内 `modex_harbor_entry.py` 从 env 完整自建 —— 复刻 `agent_harness.py` 既有模式(`_eval_observability()` env 驱动 + `build_runtime_services` + eval toolset preset);terminal/bash 工具直接操作容器自身 FS;max_turns/timeout 经 harbor trial 参数;不做任何宿主↔容器逐命令 RPC。

**❸ Tracing(collector 直连,FILE 回退正式砍掉)**:OTel exporter → `host.docker.internal:4318`(env `MODEX_OTLP_ENDPOINT` 参数化;egress 受限任务用 harbor 现成 `--allow-agent-host` 白名单,不造机制)。FILE 回退不实现 —— "若仍简单则纳入"条件不成立:collector 路径 L2/L3 双实测通过,FILE 反需新增容器内打包/回收/离线回放三件套,复杂度反超。(map 雾区对应项勾销)

**❹ 产物与判决引用**:容器内产物全落 `/logs/agent/`(instruction-rendered / trajectory.jsonl 逐 turn / usage.json / summary.md)+ console stdout tee → harbor 自动回收(spike Q5 实证)。官方 verifier `result.json` 为只读真值:宿主侧采集脚本读 trial 目录,按契约 08 注入 `verdict_terminal-bench`(experiment=`terminal-bench.{run-id}`,instance=task instance,comment 带 run_ref → trial 目录);**verdict 注入义务在本票采集脚本**。

**❺ 归属(bot_project eval 包,dev-only 零产品暴露)**:适配器落 `examples/bot_project/bot/eval/harbor/`(entry 脚本随 tar 进容器);宿主 CLI = `python -m bot.eval.cli harbor run/judge/collect` 子命令族;不注册 HOOK 插槽、不进 roster、不进打包(installer extra 固定 `.[webui,dev]`);依赖即现有 `harbor` extra。

**实现切片**:①install() 三档引导 + 记忆工作区挂载 ②源码 tar 打包器(含 modex_graph + entry)③容器内 entry(env→agent 自建 + 工具集 + 产物写入)④OTel 指向参数化(语义映射键补 model/usage,spike L3 发现)⑤宿主 CLI 子命令族 ⑥verdict 采集脚本(契约 08 注入)⑦`--timeout-multiplier`/网络降级预案(verifier uv 自举:预装 uv 的 compose overlay 或 `--extra-docker-compose`)。

**Erratum(2026-08-20 design-closure 实测复核,本机 4.11.0 events_only)**:

- **experiment 联动机制(切片③④补)**:events_only 模式下 `POST /api/public/dataset-run-items` 是**空操作桩**——返回稳定 run id 但不写任何数据(4.11.0 源码 `dataset-run-items.ts` 明示 + 本机 `dataset_run_items_rmt` 0 行实测)。trial trace 进 experiment 的**唯一路径**:容器内 entry 的根 span 携带 `langfuse.experiment.{id,name,dataset.id,item.id}` OTLP 属性(值经 env 注入,如 `MODEX_EXPERIMENT_NAME`/`MODEX_EXPERIMENT_DATASET_ID`/`MODEX_EXPERIMENT_ITEM_ID`;稳定 experiment id 先经 stub 端点取回再注入属性——SDK run_experiment 即此机制的参照实现)→ OTLP 摄入合成 dataset_run_item 事件 → experiment 在 compare 出现。
- **trace_id 回传(切片⑥补)**:entry 将每轮 `trace_id` 写入 `/logs/agent/` 工件(并入 usage.json 或独立 trace-ids 记录);宿主采集脚本凭 trial→trace_id 映射将 `verdict_terminal-bench` score 注到对应 trace(score 挂 traceId;experiment 归集经 trace-join,同票 14 消融臂)。
