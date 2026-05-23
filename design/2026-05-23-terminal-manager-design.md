# Terminal Manager 设计文档

> 日期: 2026-05-23
> 状态: 已批准，待实现

---

## 一、设计原则

1. **依赖成熟库，避免造轮子**
   - Windows PTY: `pywinpty>=0.5.0`（微软官方维护，VS Code 终端底层）
   - Unix PTY: `pexpect>=4.0`（20年历史，广泛使用）
   - 自定义代码 < 300 行，只做薄封装层

2. **抽象先行，实现可替换**
   - `TerminalBackend` ABC: 隐藏平台差异和 I/O 细节
   - `ShellExecutor` ABC: 让 ShellTool 可在"独立子进程"和"有状态会话"间切换
   - 后续新增显示窗口、远程 SSH、Docker 容器等执行方式，只需新增实现类

3. **对 LLM 完全透明**
   - ShellTool 的参数和描述不变，LLM 无感知执行策略切换
   - TerminalTool 是独立工具，LLM 显式调用以管理终端

---

## 二、架构总览

```
┌─────────────────────────────────────────────────────────────────────┐
│                           BotService                                 │
│  ┌──────────────┐  ┌──────────────────┐  ┌──────────────────────┐   │
│  │ ShellTool    │  │ TerminalTool     │  │ TerminalManager      │   │
│  │ ┌──────────┐ │  │                  │  │ ├─ session["tab-1"]  │   │
│  │ │ ShellExec│ │  │ open/close/list │  │ ├─ session["tab-2"]  │   │
│  │ │  ──────  │ │  │ select/history  │  │ ├─ session["ssh"]    │   │
│  │ │Subprocess│ │  │                  │  │ └─ default_terminal  │   │
│  │ │  or      │ │  │                  │  │                      │   │
│  │ │Terminal  │ │  │                  │  │ LRU 驱逐 + 持久化    │   │
│  │ │Session   │ │  │                  │  │ (JSON state)         │   │
│  │ └──────────┘ │  │                  │  └──────────────────────┘   │
│  └──────────────┘  └──────────────────┘                             │
│                                    │                                  │
│                           ┌────────▼────────┐                       │
│                           │ TerminalSession │                       │
│                           │ ┌────────────┐  │                       │
│                           │ │ Terminal   │  │                       │
│                           │ │ Backend    │  │                       │
│                           │ │  (ABC)     │  │                       │
│                           │ └─────┬──────┘  │                       │
│                           └───────┼─────────┘                       │
│                                   │                                  │
│            ┌──────────────────────┼──────────────────────┐          │
│            │                      │                      │          │
│    ┌───────▼────────┐   ┌────────▼────────┐             │          │
│    │ WindowsPty     │   │ UnixPty         │             │          │
│    │ Backend        │   │ Backend         │             │          │
│    │ (pywinpty)     │   │ (pexpect)       │             │          │
│    └────────────────┘   └─────────────────┘             │          │
│                                                         │          │
│    ┌────────────────────────────────────────────────────┘          │
│    │ [EXTENSION] Phase 2+                                            │
│    │ - WindowBackend (visible terminal window)                       │
│    │ - TmuxBackend (multi-client tmux session)                       │
│    │ - RemoteBackend (asyncssh/paramiko remote shell)                │
│    └─────────────────────────────────────────────────────────────────┘
└─────────────────────────────────────────────────────────────────────┘
```

---

## 三、核心抽象层

### 3.1 TerminalBackend

```python
class TerminalBackend(ABC):
    """终端后端抽象 — 包装成熟库的薄层。

    EXTENSION: Phase 2+ 可见窗口不需要新 ABC。
      在 PtyBackend 增加 visible: bool 参数即可:
      - Windows: pywinpty 支持 ConPTY 可见模式
      - Unix: pexpect 启动 xterm/Terminal.app 并桥接 I/O
    """

    @abstractmethod
    async def start(self, shell: str | None = None,
                    cwd: str | None = None,
                    env: dict | None = None) -> None:
        """启动 shell 进程。"""

    @abstractmethod
    async def write(self, data: str) -> None:
        """向 PTY 发送输入。"""

    @abstractmethod
    async def read(self, timeout: float = 5.0,
                   max_size: int = 65536) -> str:
        """非阻塞读取 PTY 输出，超时返回已收集内容。"""

    @abstractmethod
    async def is_alive(self) -> bool: ...

    @abstractmethod
    async def terminate(self) -> None: ...  # SIGTERM / graceful

    @abstractmethod
    async def kill(self) -> None: ...       # SIGKILL / force
```

### 3.2 ShellExecutor

```python
class ShellExecutor(ABC):
    """Shell 执行策略抽象。

    EXTENSION: Phase 2+ 可扩展:
      - RemoteExecutor: asyncssh/paramiko 远程执行
      - DockerExecutor: 在容器内执行
    """
    @abstractmethod
    async def execute(self, command: str,
                      working_dir: str | None = None,
                      timeout: int = 60) -> str: ...

class SubprocessExecutor(ShellExecutor):
    """当前行为 — 每次独立 subprocess，无状态，可靠 fallback。"""

class TerminalSessionExecutor(ShellExecutor):
    """有状态会话执行 — 通过 TerminalManager 执行，cd/env 持久化。"""
```

---

## 四、平台实现

### 4.1 Shell 检测优先级

| 平台 | 优先级 | 检测方式 |
|------|--------|----------|
| Windows | **bash** > **powershell** > **cmd** | `shutil.which()` + 实际执行验证 |
| Linux | **bash** > **sh** | `$SHELL` > `shutil.which("bash")` > "/bin/sh" |
| macOS | **bash** > **zsh** > **sh** | `$SHELL` > `shutil.which("bash")` > `shutil.which("zsh")` > "/bin/sh" |

**Windows bash 检测**（需求要求"实际执行检测"）：
1. `shutil.which("bash")` 找到路径
2. 执行 `bash --version`，检查返回码 0 且输出包含 "bash"
3. 通过则使用，否则继续检测 powershell，再 fallback 到 cmd

### 4.2 WindowsPtyBackend (pywinpty 包装)

```python
class WindowsPtyBackend(TerminalBackend):
    """Windows PTY — pywinpty 薄封装。

    核心代码 < 40 行，所有 PTY 协议细节由 pywinpty 处理。
    同步 API 通过 asyncio.run_in_executor 包装。
    """
```

### 4.3 UnixPtyBackend (pexpect 包装)

```python
class UnixPtyBackend(TerminalBackend):
    """Unix PTY — pexpect 薄封装。

    核心代码 < 35 行，所有 PTY 协议细节由 pexpect 处理。
    pexpect.read_nonblocking 是非阻塞的，天然适合 async 包装。
    """
```

### 4.4 跨平台工厂

```python
def create_pty_backend() -> TerminalBackend:
    """根据平台自动选择 backend，调用方无感知。"""
    if sys.platform == "win32":
        import pywinpty
        return WindowsPtyBackend()
    else:
        import pexpect
        return UnixPtyBackend()
```

---

## 五、ShellTool 增强

### 5.1 向后兼容的增强

ShellTool 构造函数增加可选的 `executor` 参数：

```python
class ShellTool(Tool):
    def __init__(self,
                 executor: ShellExecutor | None = None,
                 timeout: int = 60,
                 enable_safety_guard: bool = True):
        self._executor = executor or SubprocessExecutor()
        # ... 其余不变

    async def execute(self, command: str,
                      working_dir: str | None = None, **kwargs) -> str:
        return await self._executor.execute(command, working_dir,
                                            timeout=self.timeout)
```

### 5.2 动态 Description 生成（关键：LLM 平台感知）

ShellTool 的 `description` 属性必须**在注册时动态生成**，告知 LLM 当前实际使用的 shell 类型，避免跨平台命令差异。

```python
class ShellTool(Tool):
    @property
    def description(self) -> str:
        """动态生成描述，包含具体 shell 类型和平台提示。"""
        shell_info = self._executor.shell_info()  # 返回 ShellInfo 对象
        parts = [
            f"Execute a shell command using {shell_info.name} "
            f"and return its output."
        ]

        # 平台/Shell 特定提示（避免 LLM 生成错误命令）
        if shell_info.name == "bash":
            parts.append(
                "Commands run in bash. Use POSIX syntax: forward slashes for paths, "
                "single quotes for strings, && for chaining."
            )
        elif shell_info.name == "powershell":
            parts.append(
                "Commands run in PowerShell. Use PowerShell syntax: "
                "Get-ChildItem instead of ls, semicolons for chaining, "
                "backtick for line continuation."
            )
        elif shell_info.name == "cmd":
            parts.append(
                "Commands run in Windows CMD. Use CMD syntax: backslashes for paths, "
                "&& for chaining, %VAR% for environment variables."
            )
        elif shell_info.name == "zsh":
            parts.append(
                "Commands run in zsh. Compatible with bash syntax."
            )

        # 状态提示（有状态 vs 无状态）
        if shell_info.is_stateful:
            parts.append(
                "This is a stateful session: cd, environment variables, "
                "and aliases persist across commands."
            )
        else:
            parts.append(
                "Each command runs in a fresh process: cd and environment "
                "changes do NOT persist."
            )

        if self.enable_safety_guard:
            parts.append("Safety guard is enabled.")
        return " ".join(parts)
```

**ShellInfo 数据类**：

```python
@dataclass(frozen=True)
class ShellInfo:
    name: str           # "bash", "powershell", "cmd", "zsh", "sh"
    path: str           # 可执行文件完整路径
    platform: str       # "windows", "linux", "darwin"
    is_stateful: bool   # True: TerminalSession, False: Subprocess
```

**各 Executor 的 shell_info() 实现**：

```python
class SubprocessExecutor(ShellExecutor):
    def shell_info(self) -> ShellInfo:
        # 检测当前平台的默认 shell（同 TerminalBackend 检测逻辑）
        return _detect_platform_shell()  # 复用检测函数

class TerminalSessionExecutor(ShellExecutor):
    def shell_info(self) -> ShellInfo:
        # 从 TerminalManager 的默认会话获取实际 shell
        session = self._tm.get_default_session()
        return session.shell_info  # TerminalSession 持有 ShellInfo
```

**为什么这个设计重要**：
- LLM 不知道底层用的是 bash 还是 PowerShell，会生成 `ls` 给 CMD 用，导致失败
- 动态 description 在 tool schema 注册时注入，LLM 根据 description 调整命令语法
- 即使同一台机器，TerminalSession 用 bash 而 SubprocessExecutor 用 cmd 时，description 也会正确反映

### 5.2 BotService 集成

```python
# bot/service/builders.py
def _make_shell_tool(terminal_manager=None, timeout=60,
                     enable_safety_guard=True) -> Tool:
    if terminal_manager is not None:
        executor = TerminalSessionExecutor(
            terminal_manager=terminal_manager,
            default_terminal="default",
        )
    else:
        executor = SubprocessExecutor()
    return ShellTool(executor=executor, timeout=timeout,
                     enable_safety_guard=enable_safety_guard)
```

**fallback 策略**：TerminalManager 初始化失败（依赖缺失、配置禁用）时，自动降级为 `SubprocessExecutor`，Bot 仍可启动，只是无状态。

---

## 六、TerminalManager

### 6.1 职责

- 维护命名终端会话集合（`name -> TerminalSession`）
- 默认终端：ShellTool 未指定名称时使用
- LRU 驱逐：新建时超过 `max_terminals` 关闭最久未使用的
- 惰性存活检测：只在 `execute()` 前检查，不轮询
- 持久化/恢复会话元数据和历史

### 6.2 配置项

| 配置 | 默认值 | 说明 |
|------|--------|------|
| max_terminals | 5 | 最大终端数 |
| history_count | 5 | 每个终端保留最近 N 条命令记录 |
| history_truncate | 200 | 命令/输出各截断字符数 |
| storage_dir | data/terminals | 持久化目录 |
| default_timeout | 60 | 命令执行默认超时 |

### 6.3 核心 API

```python
class TerminalManager:
    def get_or_create(self, name: str,
                      cwd: str | None = None) -> TerminalSession: ...
    def get(self, name: str) -> TerminalSession | None: ...
    def close(self, name: str) -> bool: ...
    def list(self) -> list[TerminalInfo]: ...
    def select_default(self, name: str) -> None: ...
    def get_history(self, name: str) -> list[CommandRecord]: ...
    async def save_state(self) -> None: ...
    async def load_state(self) -> None: ...
```

---

## 七、TerminalSession

### 7.1 职责

- 持有单个 `TerminalBackend` 实例
- 封装 "发送命令 → 循环读取输出 → 直到 prompt 出现或超时" 的完整流程
- 维护命令历史（截断后）
- 追踪最后活跃时间（用于 LRU 驱逐）

### 7.2 execute 流程

```python
async def execute(self, command: str, timeout: float = 60) -> str:
    """执行命令，返回输出。

    流程:
    1. 检查 backend 存活，死亡则重新 start（重启恢复）
    2. 发送命令 + \\n 到 PTY
    3. 循环读取输出，直到超时或检测到 prompt 模式
    4. 记录历史（截断）
    5. 更新 last_active
    """
```

---

## 八、TerminalTool

LLM 可见的终端管理工具，参数：

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| action | enum | 是 | `open`, `close`, `list`, `select`, `history` |
| name | string | 视 action | 终端名称（open 可选，其他必填） |
| cwd | string | 否 | open 时指定初始工作目录 |

示例调用：
```
terminal_manager(action="open", name="server-ssh")
terminal_manager(action="select", name="server-ssh")
shell(command="ssh user@server")
```

---

## 九、持久化

### 9.1 存储格式 (JSON)

```json
{
  "version": 1,
  "default_terminal": "tab-1",
  "sessions": [
    {
      "name": "tab-1",
      "shell_type": "bash",
      "cwd": "/home/user/project",
      "env": {"KEY": "value"},
      "created_at": 1234567890.0,
      "last_active": 1234567900.0,
      "history": [
        {
          "command": "ls -la",
          "output": "total 128...",
          "exit_code": 0,
          "timestamp": 1234567895.0
        }
      ]
    }
  ]
}
```

### 9.2 恢复策略

**不保存进程本身**（无法序列化），只保存元数据。重启后：
1. 从 JSON 加载会话列表
2. 每个会话标记为 `needs_restart=True`
3. 首次 `execute()` 时自动重新 `start()`，恢复 cwd 和 env

---

## 十、BotService 集成

在 `BotService.initialize()` 中，ToolManager 创建后、注册标准工具前插入：

```python
# 1. 创建 TerminalManager
self.terminal_manager = TerminalManager(
    storage_dir=self._resolve_path("terminals_dir", "data/terminals"),
    max_terminals=getattr(self._app_config, 'terminal', {}).get('max_terminals', 5),
    history_count=5,
    history_truncate=200,
)
await self.terminal_manager.load_state()

# 2. 注册 TerminalTool
self.tool_manager.register(TerminalTool(self.terminal_manager))

# 3. 注册增强版 ShellTool
shell_tool = _make_shell_tool(
    terminal_manager=self.terminal_manager,
    timeout=60,
)
self.tool_manager.register(shell_tool)
```

---

## 十一、依赖

```toml
# pyproject.toml
[project.optional-dependencies]
terminal = [
    "pywinpty>=0.5.0; sys_platform == 'win32'",
    "pexpect>=4.0; sys_platform != 'win32'",
]
```

**运行时检测**：
- 依赖缺失时 TerminalManager 初始化失败
- BotService 捕获失败，自动降级为 `SubprocessExecutor`
- 记录 warning 日志提示用户安装依赖

---

## 十二、文件清单

```
framework/tools/terminal/
├── __init__.py
├── manager.py              # TerminalManager（~100 行）
├── session.py              # TerminalSession, TerminalInfo, CommandRecord（~50 行）
├── tool.py                 # TerminalTool（~60 行）
├── state_store.py          # JsonTerminalStateStore（~40 行）
└── backends/
    ├── __init__.py
    ├── base.py             # TerminalBackend ABC（~30 行）
    ├── factory.py          # create_pty_backend()（~15 行）
    ├── windows_pty.py      # WindowsPtyBackend（~40 行）
    └── unix_pty.py         # UnixPtyBackend（~35 行）

framework/tools/standard/shell_tool.py  # 修改: +ShellExecutor +SubprocessExecutor +TerminalSessionExecutor（~50 行新增）

bot/service/builders.py  # 修改: _make_shell_tool 支持注入 TerminalManager（~10 行修改）
bot/service/core.py      # 修改: initialize() 集成 TerminalManager（~15 行新增）
```

**总计新增自定义代码 < 300 行**。

---

## 十三、验收标准

| # | 标准 | 验证方式 |
|---|------|----------|
| 1 | Windows 上 ShellTool 自动检测并使用 Git Bash（若存在） | 单元测试: mock shutil.which 返回 bash 路径，验证 execute 调用 pywinpty.spawn(bash) |
| 2 | 终端管理工具可以 open/close/list/select | 集成测试: 调用 TerminalTool.execute() 各 action |
| 3 | 打开终端是真实进程（pywinpty/pexpect），非 subprocess.run | 进程列表检查 |
| 4 | 终端数量限制为 5 个，超过自动关闭最久未使用 | 单元测试: 连续 open 6 个，验证第 1 个被关闭 |
| 5 | 终端状态和历史持久化到 JSON | 检查 data/terminals/state.json |
| 6 | 重启后恢复终端列表（惰性恢复） | 停止 Bot，修改 JSON，重启后首次 execute 触发 start() |
| 7 | cd 和环境变量在会话内持久化 | 测试: shell("cd /tmp"); shell("pwd") 返回 /tmp |
| 8 | 跨平台: Windows/Linux/macOS | CI 分别在三个平台运行测试 |
| 9 | 依赖缺失时自动降级为 SubprocessExecutor | 测试: 卸载 pywinpty/pexpect，验证 Bot 仍可启动 |

---

## 十四、Phase 2+ 扩展方向（预留接口）

### 14.1 可见终端窗口

不需要新 ABC。在 `PtyBackend` 增加 `visible: bool` 参数：

```python
# Windows: pywinpty 支持 ConPTY 可见模式
WindowsPtyBackend(visible=True)  # 启动 conhost.exe 窗口

# Unix: pexpect 启动 xterm/Terminal.app
UnixPtyBackend(visible=True)     # 启动 xterm -e ...
```

### 14.2 用户参与控制

在 `TerminalSession` 上叠加并发控制（不影响 Phase 1）：

```python
class TerminalSession:
    # EXTENSION: Phase 2+ 并发控制
    # _lock: asyncio.Lock = field(default_factory=asyncio.Lock)  # 独占式
    # _input_queue: asyncio.Queue = field(default_factory=asyncio.Queue)  # 队列式

    # async def inject_user_input(self, text: str) -> None:
    #     """外部用户输入注入，与 LLM 命令进入同一队列。"""
```

### 14.3 远程执行

新增 `RemoteExecutor`：

```python
class RemoteExecutor(ShellExecutor):
    """通过 asyncssh 在远程服务器执行命令。
    TerminalManager 在本地管理多个到不同服务器的连接。
    """
```

### 14.4 tmux 后端

新增 `TmuxBackend(TerminalBackend)`：复用 tmux 会话，天然支持多客户端连接。

---

## 十五、风险与缓解

| 风险 | 缓解 |
|------|------|
| pywinpty/pexpect API 不兼容 | 通过 TerminalBackend ABC 隔离，更换库只需修改 < 40 行 |
| PTY 输出读取不可靠（无明确分隔符） | 使用超时截断 + prompt 启发式检测，不完美但可用；后续可优化 |
| Windows 上 bash 检测失败 | 明确的 fallback 链: bash > powershell > cmd，测试覆盖 |
| 依赖缺失导致 Bot 无法启动 | 自动降级为 SubprocessExecutor，记录 warning |
| 持久化文件损坏 | JSON decode 失败时重置为全新状态，不阻塞启动 |
