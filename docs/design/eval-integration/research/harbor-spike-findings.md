# Harbor 可行性 spike — 实测结论(2026-08-20)

> 票 01-harbor-feasibility-spike 的执行存证。执行环境:macOS(Docker Desktop 29.2.0)/ 仓库 venv Python 3.12.10 / harbor 0.21.0。
> 本次执行零 git 变更、零仓库文件修改(本文件为唯一交付物);throwaway 探针代码全部位于 `/var/folders/.../T/opencode/harbor-spike/`(PYTHONPATH 注入,不入仓库)。
> 所有判定均附实测命令与裁剪输出。

## 总判定:GO(带已记录的网络降级)

L1 结构验证 ✅ → L2 全链路契约(fake provider)✅ → L3 真实模型单任务 ✅(reward 1.0)。
所有降级均为**本机网络环境属性**(apt 慢、pypi.org TLS 阻断、github release 下载失败),非 harbor 契约或框架阻断;适配器(ticket 02)需把"镜像源可配置"纳入设计。

---

## 执行阶梯记录

### L1 结构验证($0)— PASS

harbor 0.21.0 无 `--dry-run` 旗标;`--print-config`(解析 JobConfig 后退出)是其结构等价物,另用 `AgentFactory` 直接验证 agent 导入装配:

```
$ uv pip install --python .venv/bin/python harbor
Resolved 83 packages in 1.51s
Installed 1 package in 418ms
 + harbor==0.21.0                      # 仓库 venv 依赖零冲突,仅新增 harbor 本体

$ PYTHONPATH=. harbor run --print-config -a modex_spike_agent:ModexSpikeAgent -p hello-world ...
{ "jobs_dir": ".../jobs",
  "agents":  [ { "name": "modex_spike_agent:ModexSpikeAgent" } ],
  "datasets":[ { "path": "hello-world" } ] }   # 自定义 agent 导入路径 + 本地数据集均解析成功

$ PYTHONPATH=. python -c "...AgentFactory.create_agent_from_config(cfg, logs_dir=...)"
constructed: ModexSpikeAgent
agent name(): modex-spike
extra env visible: 1                    # --ae 注入的 env 经 _get_env() 可见(供凭据注入路径)
```

demo 数据集来自官方 registry:`harbor download harbor/hello-world --overwrite` → `Successfully downloaded 1 task(s)`(经 hub.harborframework.com / Supabase,2s)。

### L2 全链路契约($0)— 核心通过;verifier 因网络降级

跑法:`PYTHONPATH=. harbor run -a modex_spike_agent:ModexSpikeAgent -p hello-world --n-concurrent 1 -y`(v2);verifier 超时后 v3 追加 `--timeout-multiplier 6`。探针为确定性 fake provider(纯 POSIX shell,无 LLM):install() 记录容器事实,run() 解任务 + 向 `host.docker.internal:4318` 发真实 OTLP POST + 写 `/logs/agent/` 工件。

- **agent 链路全通**:trial 结果目录回收全部 4 个工件(`agent/env-facts.txt`、`agent/instruction-rendered.txt`、`agent/otel-probe.txt`、`agent/trace-summary.json`);`instruction-rendered.txt` 内容即任务指令原文 → `@with_prompt_template` 渲染穿透实证。
- **OTLP 实证**(`agent/otel-probe.txt`):
  ```
  HTTP/1.1 200 OK ... {"partialSuccess":{}}
  OTLP_ENDPOINT http://host.docker.internal:4318/v1/traces
  ```
  且该 span 在 Langfuse 可查:`GET /api/public/v2/observations` → `SPAN | modex.spike.otlp_probe | 2026-08-20T09:14:12.379Z`。
- **verifier 降级(网络属性,非契约失败)**:hello-world 的 test.sh 需 `apt-get install curl` + 从 github releases 下载 uv。实测:
  - 第 1 次(默认 120s):agent 侧 `ensure_system_dependencies` 走 apt 安装 python3,`apt-get update` 连 `ports.ubuntu.com:80` 超时 → NetworkConnectionError(暴露 ubuntu:24.04 无 python3,见 Q2;探针随即改为纯 shell)。
  - 第 2 次:agent 链路全通,verifier 120s 超时(apt 慢)。
  - 第 3 次(×6 multiplier,总 4m21s):apt+curl 装成,但 uv 下载失败 → `curl: (56) Failure when receiving data from the peer`(github release 资产下载被阻断)→ uvx 缺失 → pytest 未跑 → reward 0.0。任务本身已被 agent 解出(`hello.txt` 内容正确)。
- 首跑 litellm 告警佐证网络面:`Failed to fetch remote model cost map from https://raw.githubusercontent.com/...: timed out. Falling back to local backup.`

### L3 真实模型单任务(~$0.01 级,远低于 $0.5-1 预算)— PASS

scratch 自建一次性任务 `l3-task`(`FROM python:3.12-slim` + 镜像源预装 openai/otel SDK;离线 bash 判定器),真实 provider 用 `examples/bot_project/.env` 的 stepfun 凭据经 `--ae` 注入:

```
$ harbor run -a modex_spike_real_model_agent:ModexSpikeRealModelAgent -p l3-task \
    --ae LLM_API_KEY=*** --ae LLM_BASE_URL=https://api.stepfun.com/step_plan/v1 \
    --ae LLM_MODEL=openai/step-3.7-flash ...
┃ Reward ┃ 1.0 │ 1 ┘     # 真实模型(step-3.7-flash)在容器内作答 "banana",判定通过
Total runtime: 24s
```

- **env 注入**:`usage.json`/`run-stdout.txt` 证明容器内凭据可用(`LLM_REPLY banana`)。
- **token 计量**:`populate_context_post_run` 读 `/logs/agent/usage.json` → job result `stats.n_input_tokens: 50 / n_output_tokens: 856 / cost_usd: None`(计量通路打通;成本归票 06 价格表)。
- **OTel 落 Langfuse**:容器内真实 opentelemetry SDK(BatchSpanProcessor + OTLPSpanExporter → host.docker.internal:4318)→ Langfuse 出现 `GENERATION | modex.spike.l3_llm_call | 09:27:59.080Z → 09:28:17.191Z`(span 被 Langfuse 按 gen_ai 属性识别为 GENERATION;model/usage 字段为 null,因探针用了通用属性名——ticket 02 应改用 Langfuse OTel 语义映射键)。
- 首次 L3 尝试因探针脚本 bug 失败(`get_tracer() got an unexpected keyword argument 'provider'`),修复(改 `trace.set_tracer_provider`)后通过;属探针问题,非框架问题。

---

## 五个验证问题的判定

### Q1 installed-agent 契约 — PASS

契约实证(源码 `harbor/agents/installed/base.py` + `harbor/agents/base.py`):
- `BaseInstalledAgent(BaseAgent, ABC)`:需实现 `name()`(staticmethod)、`version()`、`setup()`(基类已有默认)、`install(environment)`(abstract)、`run(instruction, environment, context)`(abstract);`@with_prompt_template` 装饰 `run()`(渲染 prompt 模板后注入,L2 已实证指令原文穿透)。
- 宿主侧工厂:`AgentFactory.create_agent_from_config` 对 `module.path:ClassName` 自定义导入路径即插即用;`--ak` kwargs、`--ae` env 均达构造路径。
- 与框架兼容性:探针即最小形态;`exec_as_agent/exec_as_root` 给容器内执行通道,`populate_context_post_run` 给 token 计量回填(L3 已实证)。**无阻塞性不匹配。**

### Q2 任务容器 Python 可用性 — PASS(事实清单型)

- **Terminal-Bench 2.1 镜像普查**(89 任务 Dockerfile 的 FROM 行):
  `41× python:3.13-slim-bookworm`、`38× ubuntu:24.04`(+1 多阶段)、`2× python:3.11-slim`、`2× python:3.10-slim-bookworm`、`2× debian:bullseye-slim`、`2× debian:13.0-slim`、`1× python:3.11`。
- **ubuntu:24.04 裸镜像无 python**(`docker run --rm ubuntu:24.04`:python3/pip3/curl/wget 全 ABSENT,仅 apt-get,root);其中仅 9/39 个 ubuntu 任务在 Dockerfile 里装了 python3,其余 30 个 agent 环境完全无 Python。verifier 侧不依赖镜像:标准 test.sh 在验证时自举 uv+pytest(需网络)。
- **python:3.13-slim-bookworm 实测**:`Python 3.13.15 / pip 26.2.1 / venv OK / Debian bookworm`(拉取经 docker 镜像源成功)。
- **含义(ticket 02)**:近半 TB 任务镜像对 agent 无 Python;适配器 install 需自举 Python(apt / uv standalone / 自带解释器 tar)或限定 Python-bearing 镜像。

### Q3 容器内 → 宿主 collector 可达性 — PASS(含一条硬约束)

- DNS:`getent hosts host.docker.internal` → `192.168.65.254`(Docker Desktop 默认解析,任务 `allow_internet=true` 即 public 网络模式,无 egress sidecar)。
- TCP + OTLP POST(ubuntu:24.04 与 python:3.12/3.13-slim 容器均测):`HTTP/1.1 200 OK`,`{"partialSuccess":{}}`。
- 端到端:L2/L3 的 span 均出现在 Langfuse(`/api/public/v2/observations` 查得,含真实 OTel SDK 路径)→ **容器内 tracing 走 collector 路径成立,无需 FILE 降级**。
- 硬约束:collector 只绑 `127.0.0.1:4318` 亦可达(host.docker.internal 走宿主回环);**Langfuse v4 会丢弃固定历史时间戳的 span**(2018 时间戳实测不入库,当前时间戳正常入库)——必须用真实墙钟时间(OTel SDK 天然满足)。若跑 egress 受限任务,harbor 提供 `--allow-agent-host host.docker.internal` 逃生门(未测,记录为票 02 事项)。

### Q4 modex_agent 最小导入面 — PASS(源码 tar + 依赖安装 + .pth 引导可行)

- 源码包:`tar czf modex-src.tar.gz src/modex_agent src/modex_graph pyproject.toml` → **3.5 MB / 1563 文件**(modex-graph 为本地 sibling、不在 PyPI,必须随包携带,ADR-0033)。
- 容器内引导(python:3.12-slim,脚本 `q4-bootstrap.sh`):`python3 -m venv` → `pip install --index-url https://pypi.tuna.tsinghua.edu.cn/simple <闭包>` → `echo /opt/modex/src > .../site-packages/modex-src.pth`:
  ```
  import modex_agent            -> /opt/modex/src/modex_agent/__init__.py
  import modex_agent.trace      -> OK
  import modex_agent.tools.terminal -> OK
  import modex_graph            -> /opt/modex/src/modex_graph/__init__.py
  BOOTSTRAP_OK
  ```
- 闭包(pyproject 只读解析):核心 14 依赖 + `llm` extra(litellm/openai)+ `terminal` extra(pexpect/libtmux)+ `observability` extra(otel-sdk/exporter;`modex_agent.trace` 的 otel 导入为惰性,但真实 tracing 需要)。`.pth` 引导成立,无需重新打包。
- **网络硬事实**:容器内 pypi.org/files.pythonhosted.org **TCP 通但 TLS 被重置**(pip 直连报 "from versions: none"),清华 tuna 镜像实测可用 → 容器内依赖安装必须走可达镜像源(仓库 `[tool.uv.index]` 已是 tuna,ticket 02 应将 index URL 参数化)。

### Q5 产物路径约定 — PASS

- agent 工件写 `/logs/agent/**` → trial 结束自动回收至宿主 `<jobs-dir>/<job>/<trial>/agent/`(docker 环境 /logs 为 bind mount,`prepare_logs_for_host` chown 回宿主用户);L2/L3 各 4-5 个文件全部实证回收。
- verifier 侧对应 `/logs/verifier/`(reward.txt / test-stdout.txt 实证);task.toml 声明的 artifacts → `<trial>/artifacts/` + `manifest.json`。
- **映射结论**:ModexAgent 的 trace summary / usage / 摘要输出落 `/logs/agent/` 即被自动回收,无需额外机制;按需另以 task.toml `[[artifacts]]` 收任务产物。

---

## 风险清单(新发现)

1. **registry 分层可达性**(高危面已探明):
   - ❌ `raw.githubusercontent.com` 超时(legacy 默认 registry URL 与 litellm cost map 拉取均受影响;harbor 0.21 实际默认走 Supabase/Hub,不受阻)。
   - ✅ `hub.harborframework.com` + Supabase registry(dataset/task 下载、89 任务 18s 拉全)。
   - ✅ docker.io 拉取经宿主已配镜像源(`docker.1ms.run` 等)正常(ubuntu/python 镜像均成)。
   - ❌ github release 资产下载容器内失败(curl 56)→ 标准 TB verifier 的 uv 自举不可用 → **跑真 TB 数据集需先解决 verifier 网络**(镜像预热/自带 uv 的 overlay/离线 verifier),否则 reward 恒 0(纯网络假象)。
2. **容器内 pypi.org TLS 阻断**:必须镜像源(见 Q4)。
3. **apt(ports.ubuntu.com:80)极慢/抖动**:hello-world verifier 需 6× timeout 才走完 apt;预算 timeout multiplier 或预建镜像。
4. **Langfuse 丢弃固定历史时间戳**(见 Q3)。
5. 镜像源一致性:镜像内 pip 与宿主 uv 都指向 tuna,避免半途直连 pypi 的组件(如某些包的 runtime 下载器)。

## 建议(文本,不落任何仓库文件改动)

- **pyproject**:无需任何改动。`harbor = ["harbor>=0.21"]` extra 已存在(pyproject.toml:99);本次 `uv pip install --python .venv/bin/python harbor` 零冲突装入,证明票内"框架侧可选 extra"路径成立。
- **MAP.md Decisions 补行(建议文本)**:"Harbor 可行性 spike — GO:installed-agent 契约/工件回收/容器→collector→Langfuse 全链实测通过;降级项为本机网络(容器内 pypi TLS 阻断、github release 下载失败、apt 慢)→ 票 02 落地时镜像源参数化 + verifier 网络策略;源码 tar+venv+.pth 引导 3.5MB 可行(含本地 modex-graph)。"
- **ticket 02 适配器要点(实测支撑)**:① 继承 `BaseInstalledAgent`,`name()` staticmethod + async `install()`/`run()` + `@with_prompt_template`;② install 自举 = venv + 源码 tar(含 modex_graph)+ `.pth`,index URL 必须可配(tuna);③ 无 Python 镜像(30/39 的 ubuntu:24.04 任务)需自举解释器或限定镜像,建议先支持 python:*-slim 类;④ OTel 用真实时间戳 + Langfuse 语义映射键(补 model/usage 字段);⑤ 工件全落 `/logs/agent/`;⑥ token 计量走 `populate_context_post_run`(通路已实证);⑦ 凭据经 `--ae` 注入已实证;⑧ TB 真 verifier 跑分前先解 uv 下载网络问题(可预装 uv 的 compose overlay 或 `--extra-docker-compose`)。
- **遗留(票 02 前置)**:ghcr.io 拉取未实测;`--allow-agent-host` 于 egress 受限任务未测;Langfuse v4 events_only 模式下 trace 级 API 受限(observations v2 可用)。

## 附:实测命令与产物索引

| 项 | 位置 |
|---|---|
| 探针代码(throwaway) | `$TMP/opencode/harbor-spike/modex_spike_agent.py`、`modex_spike_real_model_agent.py`、`q4-bootstrap.sh`、`l3-task/` |
| L1 | `l1-print-config.json` |
| L2 | `jobs/spike-l2-fake-v2/`(超时)、`jobs/spike-l2-fake-v3/`(reward 0,网络)、`l2-run*.log` |
| L3 | `jobs/spike-l3-real-v2/`(reward 1.0)、`l3-run*.log` |
| Q4 | `modex-src.tar.gz`(3.5MB)+ 容器内 `BOOTSTRAP_OK` 输出 |
| TB-2.1 普查 | `tb21/terminal-bench-2-1/`(89 任务) |
