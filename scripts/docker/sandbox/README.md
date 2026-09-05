# modex-sandbox — OCI 家族基准镜像

ADR-0007 sandbox 集成的 OCI 家族配件（非全局前提）。`OciContainerRuntime`
（Ticket 07）的容器基于本镜像：debian-slim + 最小工具集，非 root 用户 `modex`
(uid 1000)，工作目录 `/workspace`。

镜像只装环境，不定义入口 —— 容器由 runtime 以 `sleep infinity` 启动常驻，
命令经 `docker exec` argv 数组注入（禁止 `sh -c` 拼接）。

工具集（刻意最小）：bash、python3、git、ripgrep、jq、curl（探针/健康检查）、
ca-certificates（https 探针前提）。

## 构建

```bash
scripts/docker/sandbox/build.sh                 # → modex-sandbox:latest
scripts/docker/sandbox/build.sh --tag foo:bar   # 自定义 tag
```

首次启用 oci 档位前 build 一次即可，之后所有容器共用。

## 沙箱内缺依赖：三条路

按优先级：

1. **工作区 venv / node_modules 直通**（最常用）—— workspace 经 bind mount
   同路径挂载进容器，依赖装在 workspace 内（`.venv`、`node_modules`），跨命令
   持久、与宿主共享，无需改镜像。
2. **改 Dockerfile 重 build** —— 确需系统级工具（如 `ffmpeg`）时，往上面的
   最小 apt 清单加包，重新 `build.sh`，重启容器。
3. **该命令不进沙箱** —— per-agent 策略调整（或 P2 命令路由规则），让该命令
   在 host 执行。
