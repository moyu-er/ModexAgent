# Harbor 可行性 spike

Status: closed
Labels: wayfinder:task
Blocks: ModexHarborAgent 适配器设计 (02)
Planned: 2026-08-18 — 执行计划已定(见下)
Aborted: 2026-08-20 — 首次执行中断:subagent 越界 commit+push(已软回滚并卸载 harbor)、findings 未交付
Resolved: 2026-08-20 — 重跑完成(约束收紧版),**判定 GO(带已记录网络降级)**;完整存证见 research/harbor-spike-findings.md

## Question

在真实环境验证 installed-agent 接入路径的全部前提,产出 go/no-go 结论与事实清单:

1. `harbor` CLI(py>=3.12)安装后,用 demo 数据集 dry-run + 真跑一个任务,确认 installed-agent 契约(`BaseInstalledAgent`:name()/async install()/async run(),`@with_prompt_template` 装饰)与我们的框架兼容
2. 任务容器内 Python 可用性(Terminal-Bench 2.1 常见镜像的 python3 版本、venv 能力)
3. **容器内 → 宿主 collector 网络可达性**:`host.docker.internal:4318`(macOS Docker Desktop)能否收到 OTLP POST —— 决定容器内 tracing 走 collector 路径还是降级
4. `modex_agent` 最小导入面:框架 src 包进容器需要哪些依赖(provider/terminal 工具链的最小闭包),源码 tar + 依赖安装 + `.pth` 引导是否可行
5. 产物路径约定:agent 工件必须落在 `/logs/agent` 才能被回收 —— 我们的 trace/summary 输出如何映射

验收:一张事实清单(每项有实测证据)+ go/no-go。这是一次性安装与运行,不是实现适配器 —— 适配器设计是下一张 ticket。

## 已核实前提(2026-08-18,零成本事实核查)

- **harbor 包可用**:PyPI `harbor 0.21.0`,`requires-python >=3.12`,与仓库 venv(3.12.10)兼容
- **框架最小闭包已知**:核心 15 依赖(pydantic/modex-graph/httpx/…)+ `llm` extra(litellm/openai)+ `terminal` extra(pexpect/libtmux)。容器内可行性由 spike 实测;`.pth` 源码引导不需要重新打包
- **用户安装面安全(硬约束)**:harbor 是开发者评测工具链,**绝不进用户安装面** —— 安装器固定 `.[webui,dev]` + 框架 `.[all,dev]`(install.sh:353-354 / install.bat:533-535),任何新增 extra 默认不装给用户;harbor 以框架侧可选 extra 声明(参照既有 `eval` extra 先例,仅按需 `uv sync --extra`/`uv pip install -e ".[harbor]"`),打包/安装器永远不引用它

## 执行计划(三段阶梯,决议 2026-08-18)

对齐评测框架的最佳实践形态:确定性 provider 路径正是为契约验证而设,真实模型只用于正式跑分。

1. **L1 结构验证($0,分钟级)**:`--dry-run` —— 验证 agent 参数装配、命令解析、任务解析,不起容器
2. **L2 全链路契约($0,十分钟级)**:demo 数据集 + fake provider 真跑一个任务 —— 真实容器、真实 install 引导(venv/源码 tar/.pth)、真实产物回收到 `/logs/agent`、真实 collector 网络路径。**go/no-go 主体在此产出**
3. **L3 真实模型确认(~$0.5-1,单独批准)**:单任务真实 provider —— 验证 env 注入、token 计量、OTel trace 落 Langfuse。把"框架问题"与"模型问题"分开的最终一环

L1/L2 失败即 no-go 或改道(如 collector 网络不通 → 记录降级路径,FILE 回退纳入 ticket 02 设计);L3 失败不否定接入,只标记待解问题。

**环境策略**:harbor 以框架侧可选 extra(`harbor = [...]`)装进仓库开发环境 —— spike 验证的路径即 ticket 02 实现阶段的长期路径,不造二次环境;版本下限 `0.21`。若发现依赖树与仓库冲突严重,回退一次性 venv 并在 ticket 02 重评。

**执行产出**:事实清单(5 项各带实测证据)写入本 ticket Comments + `research/` 存证,MAP Decisions so far 补一行,关闭本 ticket,解锁 ticket 02。
