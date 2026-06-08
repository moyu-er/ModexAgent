# Shell 工具体系分析与改进建议

> 2026-06-08 — 基于当前 `develop_gyt` 分支代码的完整分析

## 1. 现状概览

当前框架中存在**两套独立的命令执行体系**：

| 体系 | 工具 | 执行方式 | 状态保持 | 使用者 |
|---|---|---|---|---|
| **A. PTY 交互体系** | `terminal` + `bash`(CommandTool) + `process` | PTY 后端 (WinPTY/pexpect/tmux) | 有状态（多标签页） | 主 agent |
| **B. Subprocess 体系** | `bash`(SubprocessTool) | `asyncio.create_subprocess_shell` | 无状态（每次新进程） | subagent |

### 体系 A 的组成

```
TerminalTool   → 管理终端标签页 (open/close/list/select/interrupt/current)
CommandTool    → 在 PTY 会话中执行命令 (5 状态完成检测)
ProcessTool    → 与运行中进程交互 (write/submit/send_keys/paste/interrupt/kill)
ProcessRegistry → 进程会话追踪、输出缓冲、速度检测
5 种后端       → WinPTY / ConPTY / pexpect / tmux / visible Windows
```

### 体系 B 的组成

```
SubprocessTool  → 一次性命令执行
SubprocessExecutor → asyncio.create_subprocess_shell 封装
```

---

## 2. 两套体系共同存在的问题

### 2.1 环境变量全量泄漏

**现状**：两套体系都通过 `build_full_env()` 将整个 `os.environ` 传给子进程。

```python
# framework/tools/terminal/env.py
def build_full_env(overrides=None) -> dict[str, str]:
    env = dict(os.environ)  # ← 全量透传，包括所有 API key
    # ... 仅做 Windows PATH 注册表合并
    return env
```

```python
# framework/tools/terminal/subprocess_tool.py
class SubprocessExecutor:
    async def execute(self, command, working_dir=None, timeout=60):
        process = await asyncio.create_subprocess_shell(
            command,
            env=build_full_env(),  # ← 子进程能拿到 ANTHROPIC_API_KEY 等
        )
```

```python
# framework/tools/terminal/session.py
def _startup_env(self) -> dict[str, str]:
    from framework.tools.terminal.env import build_full_env
    return build_full_env(self._env)  # ← PTY 后端也全量透传
```

**风险**：子进程（及其启动的任何程序）可以读取到 `ANTHROPIC_API_KEY`、`OPENAI_API_KEY` 等敏感信息。如果 agent 执行了用户提供的恶意命令或被 LLM 诱导执行，这些 secret 会直接泄漏。

**建议**：改为白名单策略，只传递系统必要变量：

```python
# 改进方案：白名单 + 可配置的额外变量
def build_safe_env(
    allowed_keys: list[str] | None = None,
    overrides: dict[str, str] | None = None,
) -> dict[str, str]:
    """Build a minimal environment dict for child processes.

    Only system-critical variables are forwarded by default.
    API keys and other secrets are excluded.
    Additional keys can be allowed via configuration.
    """
    if sys.platform == "win32":
        sr = os.environ.get("SYSTEMROOT", r"C:\Windows")
        env = {
            "SYSTEMROOT": sr,
            "COMSPEC": os.environ.get("COMSPEC", f"{sr}\\system32\\cmd.exe"),
            "USERPROFILE": os.environ.get("USERPROFILE", ""),
            "HOMEDRIVE": os.environ.get("HOMEDRIVE", "C:"),
            "HOMEPATH": os.environ.get("HOMEPATH", "\\"),
            "TEMP": os.environ.get("TEMP", f"{sr}\\Temp"),
            "TMP": os.environ.get("TMP", f"{sr}\\Temp"),
            "PATHEXT": os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD"),
            "PATH": os.environ.get("PATH", ""),
            "PYTHONUNBUFFERED": "1",
            "APPDATA": os.environ.get("APPDATA", ""),
            "LOCALAPPDATA": os.environ.get("LOCALAPPDATA", ""),
        }
    else:
        env = {
            "HOME": os.environ.get("HOME", "/tmp"),
            "LANG": os.environ.get("LANG", "C.UTF-8"),
            "TERM": os.environ.get("TERM", "dumb"),
            "PATH": os.environ.get("PATH", ""),
            "PYTHONUNBUFFERED": "1",
        }

    # Allow configured extra keys
    for key in allowed_keys or []:
        val = os.environ.get(key)
        if val is not None:
            env[key] = val

    if overrides:
        env.update(overrides)
    return env
```

**影响范围**：`SubprocessExecutor.execute()` 和 `TerminalSession._startup_env()` 两处调用点。

---

### 2.2 缺少 SSRF 防护

**现状**：两套体系都没有检查命令中是否包含对内网地址的请求。LLM 可以构造类似以下的命令：

```bash
curl http://169.254.169.254/latest/meta-data/iam/security-credentials/
curl http://10.0.0.1:8080/admin/
curl http://localhost:6379/  # Redis 无密码场景
```

这在云环境（尤其是有 metadata service 的环境）中是严重的安全风险。

**建议**：在安全层新增 SSRF 检测模块，被两套体系的 guard 共同调用：

```python
# framework/security/network.py
import ipaddress
import re
import socket

_BLOCKED_NETWORKS = [
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("100.64.0.0/10"),   # carrier-grade NAT
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),   # link-local / cloud metadata
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
]

_URL_RE = re.compile(r"https?://[^\s\"'`;|<>]+", re.IGNORECASE)


def contains_internal_url(command: str) -> bool:
    """Return True if the command contains a URL targeting a private/internal IP."""
    for match in _URL_RE.finditer(command):
        url = match.group(0)
        hostname = _extract_hostname(url)
        if hostname and _resolves_to_private(hostname):
            return True
    return False


def _extract_hostname(url: str) -> str | None:
    from urllib.parse import urlparse
    try:
        return urlparse(url).hostname
    except Exception:
        return None


def _resolves_to_private(hostname: str) -> bool:
    try:
        infos = socket.getaddrinfo(hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
    except socket.gaierror:
        return False
    for info in infos:
        try:
            addr = ipaddress.ip_address(info[4][0])
            # Normalize IPv6-mapped IPv4 (e.g. ::ffff:127.0.0.1)
            if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
                addr = addr.ipv4_mapped
            if any(addr in net for net in _BLOCKED_NETWORKS):
                return True
        except ValueError:
            continue
    return False
```

集成方式：

```python
# SubprocessTool._guard_command 中新增
from framework.security.network import contains_internal_url

def _guard_command(self, command: str) -> str | None:
    # ... 现有 deny/allow 检查 ...
    if contains_internal_url(command):
        return (
            "Error: Command blocked by safety guard (internal/private URL detected). "
            "This is a security boundary — do not attempt to bypass it."
        )
    return None
```

---

### 2.3 安全拒绝缺少"不可重试"提示

**现状**：当命令被安全策略拦截时，返回的消息只是：

```python
return f"Error: Command blocked by safety guard (dangerous pattern: {pattern})"
```

LLM 收到这个消息后，可能会尝试各种绕过方式：
- 用 base64 编码命令再解码执行
- 用 symlink 跳过路径检查
- 用 `python -c` 间接执行被阻止的命令
- 用 `eval` / `bash -c` 嵌套绕过

**建议**：在所有安全拒绝消息后附加不可重试提示：

```python
_SECURITY_BOUNDARY_NOTE = (
    "\n\nNote: this is a hard security boundary, not a transient failure. "
    "Do NOT retry with shell tricks (symlinks, base64 piping, alternative "
    "tools, eval/bash -c wrapping, or other bypass attempts). "
    "If the user genuinely needs this operation, inform them and ask how to proceed."
)


def _guard_command(self, command: str) -> str | None:
    # ... existing checks ...
    if blocked:
        return f"Error: Command blocked by safety guard (reason).{_SECURITY_BOUNDARY_NOTE}"
```

---

## 3. SubprocessTool 体系（体系 B）的特有问题

### 3.1 输出截断丢失尾部关键信息

**现状**：使用尾部截断，exit code 和尾部错误信息被丢弃。

```python
# framework/tools/terminal/subprocess_tool.py
max_len = 10000
if len(result) > max_len:
    result = result[:max_len] + f"\n... (truncated, {len(result) - max_len} more chars)"
```

当输出超过 10000 字符时，`Exit code` 行（在最后拼接的）会被截断。LLM 看不到命令是否成功，也无法看到尾部的错误摘要。

**建议**：改为头尾保留策略：

```python
def _truncate_output(output: str, max_chars: int = 10_000) -> str:
    if len(output) <= max_chars:
        return output
    half = max_chars // 2
    omitted = len(output) - max_chars
    return (
        output[:half]
        + f"\n\n... ({omitted:,} chars truncated) ...\n\n"
        + output[-half:]
    )
```

### 3.2 无长驻会话支持

**现状**：SubprocessTool 每次执行都是新进程，无法：
- 启动 dev server 并后台保持
- 向运行中的进程发送 stdin
- 对长时任务做轮询

这对 subagent 场景可能是合理的（subagent 通常只需简单命令），但如果未来 subagent 需要启动 `npm run dev` 之类的长驻命令，就需要引入 session 机制。

**建议**：暂不引入，但保持接口扩展性。如果未来需要，可以参考以下模式：

```python
# 概念示意 — 仅当需求出现时实现
class ExecSessionManager:
    """Manage long-running subprocess sessions for subagents."""

    def __init__(self, max_sessions: int = 8, idle_timeout: int = 1800):
        self._sessions: dict[str, _ExecSession] = {}
        # ...

    async def start(self, command, cwd, env, timeout, yield_ms) -> tuple[str, PollResult]:
        """Start command, return (session_id, first_poll_result)."""

    async def poll(self, session_id, yield_ms) -> PollResult:
        """Poll output from running session."""

    async def write(self, session_id, chars) -> PollResult:
        """Write to session stdin."""

    async def kill(self, session_id) -> None:
        """Terminate a running session."""

    async def list(self) -> list[SessionInfo]:
        """List active sessions."""
```

关键设计要素：
- **最大会话数限制**（防止资源泄漏）
- **空闲超时自动清理**（防止僵尸会话）
- **owner 隔离**（每个 subagent 只能看到自己的会话）

### 3.3 deny/allow patterns 不够精细

**现状**：硬编码的平台特定 deny patterns，缺少配置化能力。

```python
class SubprocessTool(Tool):
    WINDOWS_DENY_PATTERNS = [
        r"\bdel\s+/[fq]\b",
        r"\brmdir\s+/s\b",
        r"\bformat\b",           # ← 太宽泛，会误杀 "git log --format=..."
        # ...
    ]
```

**问题**：
1. `\bformat\b` 会匹配 `git log --format="%h"` 中的 `format`
2. `allow_patterns` 优先于 `deny_patterns` 的逻辑没有在文档/代码中明确说明
3. 无法通过配置文件自定义

**建议**：

```python
# 改进：让 deny patterns 更精确
POSIX_DENY_PATTERNS = [
    r"\brm\s+-[rf]{1,2}\b",
    r"\bdel\s+/[fq]\b",
    r"\brmdir\s+/s\b",
    # format 只匹配作为独立命令的 format，不匹配 git --format
    r"(?:^|[;&|]\s*)format(?!=)\b",
    r"\b(mkfs|diskpart)\b",
    r"\bdd\s+if=",
    r">\s*/dev/sd",
    r"\b(shutdown|reboot|poweroff)\b",
    r":\(\)\s*\{.*\};\s*:",          # fork bomb
]
```

并明确 allow 优先的语义：

```python
def _guard_command(self, command: str) -> str | None:
    lower = command.strip().lower()

    # allow_patterns 优先：如果命中 allow，跳过所有 deny 检查
    explicitly_allowed = bool(self.allow_patterns) and any(
        re.search(p, lower) for p in self.allow_patterns
    )
    if not explicitly_allowed:
        for pattern in self.deny_patterns:
            if re.search(pattern, lower):
                return (
                    f"Error: Command blocked by safety guard (pattern: {pattern})"
                    f"{_SECURITY_BOUNDARY_NOTE}"
                )
        # 如果配置了 allowlist 但不匹配，也阻止
        if self.allow_patterns:
            return (
                "Error: Command blocked by safety guard (not in allowlist)"
                f"{_SECURITY_BOUNDARY_NOTE}"
            )
    return None
```

---

## 4. PTY 交互体系（体系 A）的特有问题

### 4.1 ProcessRegistry 缺少资源保护

**现状**：`ProcessRegistry` 没有对运行中会话数量的上限控制：

```python
class ProcessRegistry:
    def __init__(self, config=None):
        self._running: dict[str, ProcessSession] = {}
        self._finished: dict[str, ProcessSession] = {}
        # ← 没有最大会话数限制
```

如果 LLM 在循环中反复执行命令（bug 或被诱导），`_running` 和 `_finished` 字典会无限增长。

**建议**：

```python
class ProcessRegistry:
    MAX_RUNNING = 32
    MAX_FINISHED = 128

    def create(self, *, command, terminal, cwd, pid):
        if len(self._running) >= self.MAX_RUNNING:
            raise RuntimeError(
                f"Maximum running process sessions reached ({self.MAX_RUNNING})"
            )
        # ... existing creation logic ...

    def mark_exited(self, session_id, **kwargs):
        # ... existing logic ...
        # Prune finished sessions if over limit
        if len(self._finished) > self.MAX_FINISHED:
            oldest = sorted(self._finished.items(), key=lambda x: x[1].ended_at)
            for sid, _ in oldest[:len(self._finished) - self.MAX_FINISHED]:
                self._finished.pop(sid, None)
```

### 4.2 ProcessTool 缺少 close_stdin 能力

**现状**：`ProcessTool` 支持 `write`、`submit`、`send_keys`、`paste`、`interrupt`、`kill`，但没有 `close_stdin`（发送 EOF）。

对于等待 stdin EOF 的命令（如 `cat | grep pattern`、`sort < file`），没有 close_stdin 意味着进程永远不会结束。

**建议**：在 `ProcessAction` 枚举和 `ProcessTool` 中新增 `close_stdin`：

```python
class ProcessAction(StrEnum):
    WRITE = "write"
    SUBMIT = "submit"
    SEND_KEYS = "send_keys"
    PASTE = "paste"
    INTERRUPT = "interrupt"
    KILL = "kill"
    CLOSE_STDIN = "close_stdin"    # 新增
    CLEAR = "clear"
    REMOVE = "remove"
```

实现需要后端支持。对于 PTY 后端，close_stdin 的语义是关闭写端（PTY slave 的 stdin），实现方式取决于后端。可以先只在 SubprocessTool 的 session 模式中实现（如果未来引入），PTY 模式下作为 no-op 或提示不支持。

### 4.3 CommandTool 缺少安全防护

**现状**：`CommandTool.execute()` 直接执行命令，**没有任何安全检查**。没有 deny patterns，没有 SSRF 检查，没有任何 guard。

```python
class CommandTool(Tool):
    async def execute(self, command: str, **_kwargs):
        session = await self._manager.get_default()
        await session.submit_command(command)  # ← 直接执行，无 guard
```

SubprocessTool 有 `_guard_command()`，但 CommandTool 完全跳过了安全检查。这意味着主 agent（通过 CommandTool）可以执行任何危险命令，而 subagent（通过 SubprocessTool）反而被限制了。

**这是一个安全漏洞**：更强大的工具反而有更弱的安全防护。

**建议**：为 CommandTool 也添加安全 guard，可以与 SubprocessTool 共享 guard 逻辑：

```python
# 抽取为独立的 guard 函数
# framework/security/command_guard.py

class CommandGuard:
    """Shared command safety guard for both CommandTool and SubprocessTool."""

    def __init__(
        self,
        deny_patterns: list[str] | None = None,
        allow_patterns: list[str] | None = None,
    ):
        self.deny_patterns = deny_patterns or self._default_deny_patterns()
        self.allow_patterns = allow_patterns or []

    def check(self, command: str) -> str | None:
        """Return error message if command is blocked, None if allowed."""
        lower = command.strip().lower()

        explicitly_allowed = bool(self.allow_patterns) and any(
            re.search(p, lower) for p in self.allow_patterns
        )
        if not explicitly_allowed:
            for pattern in self.deny_patterns:
                if re.search(pattern, lower):
                    return (
                        f"Error: Command blocked by safety guard (pattern: {pattern})"
                        f"{_SECURITY_BOUNDARY_NOTE}"
                    )
            if self.allow_patterns:
                return "Error: Command blocked by safety guard (not in allowlist)"

        if contains_internal_url(command):
            return (
                "Error: Command blocked by safety guard (internal/private URL detected)"
                f"{_SECURITY_BOUNDARY_NOTE}"
            )
        return None
```

两套工具都使用同一个 `CommandGuard`：

```python
# CommandTool
class CommandTool(Tool):
    def __init__(self, manager, registry, config=None, guard=None):
        self._guard = guard or CommandGuard()

    async def execute(self, command: str, **_kwargs):
        if self._guard:
            error = self._guard.check(command)
            if error:
                return _build_command_xml(error, CommandResultStatus.BLOCKED, 0)
        # ... existing execution logic ...
```

### 4.4 ProcessRegistry 缺少 owner 隔离

**现状**：所有进程会话存储在全局字典中，任何 agent/subagent 可以看到所有会话。

```python
class ProcessRegistry:
    def __init__(self):
        self._running: dict[str, ProcessSession] = {}   # 全局
        self._finished: dict[str, ProcessSession] = {}   # 全局
```

在多 agent（主 agent + 多 subagent）场景下，subagent A 可能误操作 subagent B 的进程。

**建议**：为 ProcessSession 增加 `owner` 字段，查询时按 owner 过滤：

```python
@dataclass
class ProcessSession:
    id: str
    terminal: str
    command: str
    owner: str | None = None  # 新增：创建者标识
    # ... existing fields ...


class ProcessRegistry:
    def get_running_by_terminal(
        self, terminal_name: str, *, owner: str | None = None
    ) -> ProcessSession | None:
        for session in reversed(list(self._running.values())):
            if session.terminal == terminal_name:
                if owner is None or session.owner is None or session.owner == owner:
                    return session
        return None
```

### 4.5 TerminalSession.execute() 的输出没有截断保护

**现状**：`TerminalSession.execute()` 直接返回全部输出，没有截断。虽然有 `sanitize_terminal_output()` 清理 ANSI 序列，但超长输出（如 `cat large_file.log`）会完整返回给 LLM。

```python
async def execute(self, command: str, timeout: float = 60.0) -> str:
    # ... collect output_parts ...
    output = "".join(output_parts)
    output = sanitize_terminal_output(output)
    # ← 没有 max_chars 截断，直接返回
    return output
```

`CommandTool` 调用的是 `poll_until_settled()` 路径（不是 `TerminalSession.execute()`），那条路径的输出由 `ProcessRegistry` 做缓冲截断。但 `TerminalSession.execute()` 作为直接调用路径，缺少保护。

**建议**：添加截断保护：

```python
async def execute(self, command: str, timeout: float = 60.0) -> str:
    # ... existing logic ...
    output = sanitize_terminal_output(output)

    # Truncate if too long (head-tail preservation)
    max_output_chars = 50_000
    if len(output) > max_output_chars:
        half = max_output_chars // 2
        output = (
            output[:half]
            + f"\n\n... ({len(output) - max_output_chars:,} chars truncated) ...\n\n"
            + output[-half:]
        )
    return output
```

---

## 5. 改进优先级汇总

| 优先级 | 改动 | 影响体系 | 复杂度 | 安全价值 |
|---|---|---|---|---|
| **P0** | CommandTool 添加安全 guard | A | 中 | 🔴 关键漏洞 |
| **P0** | 环境变量白名单化 | A + B | 低 | 🔴 Secret 泄漏 |
| **P0** | 安全拒绝附加不可重试提示 | A + B | 低 | 🟡 防止绕过 |
| **P1** | SSRF 防护模块 | A + B | 中 | 🔴 云环境关键 |
| **P1** | SubprocessTool 头尾保留截断 | B | 低 | — 提升实用性 |
| **P1** | ProcessRegistry 会话数上限 | A | 低 | 🟡 资源保护 |
| **P1** | deny patterns 精确化 | A + B | 低 | 🟡 减少误杀 |
| **P2** | ProcessRegistry owner 隔离 | A | 中 | 🟡 多 agent 安全 |
| **P2** | TerminalSession.execute() 截断保护 | A | 低 | — 防 token 爆炸 |
| **P2** | ProcessTool close_stdin | A | 低 | — 完整性 |
| **P3** | SubprocessTool 长驻会话模式 | B | 高 | — 按需引入 |

---

## 6. 推荐的统一安全架构

两套体系的安全逻辑目前是分散的：

```
SubprocessTool._guard_command()   → 独立的 deny/allow 逻辑
CommandTool                       → 无任何安全检查
LocalSecureExecutor               → 通用安全策略（但未被终端工具使用）
```

建议统一为：

```
framework/security/command_guard.py    ← 统一的命令安全检查
  ├── deny/allow patterns
  ├── SSRF 检测
  ├── 不可重试提示
  └── (future) workspace 隔离

framework/security/network.py         ← SSRF 检测基础设施
  └── contains_internal_url()

framework/tools/terminal/env.py       ← 环境变量白名单
  └── build_safe_env()
```

这样 `CommandTool`、`SubprocessTool`、以及任何未来的执行工具都共享同一套安全逻辑：

```python
# CommandTool
guard = CommandGuard(deny_patterns=..., allow_patterns=...)
error = guard.check(command)
if error:
    return error

# SubprocessTool — 同一个 guard
error = guard.check(command)
if error:
    return error
```
