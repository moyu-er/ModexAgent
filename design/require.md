# Terminal Manager 完整需求文档

> 最后更新：2026-05-21

---

## 一、背景与目标

### 1.1 背景
- 框架提供的 shell tool 太弱，需要增强
- 需要给 bot_project 的 main agent 配置终端管理工具

### 1.2 目标
1. 改进 Shell Tool：Windows 上优先使用 Git Bash（若存在），否则 fallback
2. 实现终端管理工具：显式打开终端窗口（用户可见），通过 Shell 操作该终端
3. 跨平台支持：Windows/macOS/Linux
4. 持久化：终端状态保存到文件，重启后可恢复

---

## 二、功能需求

### 2.1 Shell Tool 改进

| 需求 | 说明 |
|------|------|
| bash 检测 | Windows 上优先检测 Git Bash（执行 `bash --version`） |
| fallback | 若无 bash，使用原来的 cmd |
| 检测方式 | 使用实际执行检测，不依赖路径判断 |

### 2.2 终端管理工具

| 操作 | 说明 |
|------|------|
| open_terminal | 打开新终端，指定名称（可选），若无则自动编号（tab-1, tab-2...） |
| close_terminal | 关闭指定终端 |
| list_terminals | 查看所有终端（名称、最后活跃时间、存活状态） |
| select_terminal | 选择后续shell默认执行的终端 |
| get_terminal_history | 获取终端命令历史(摘要, 需要额外保存记录做抽象+持久化) |

### 2.3 终端执行要求（重要）

| 需求 | 说明 |
|------|------|
| 显式打开 | 打开一个真实的终端窗口，用户能看得到 |
| 跨平台 | 各平台使用各自的默认终端, 按照列表优先级从前往后选择, 不存在才靠后后面的选择（Windows: powershell/cmd, macOS: Terminal, Linux: gnome-terminal） |
| 命令执行 | Shell 工具通过终端执行命令，返回输出 |
| 用户可见 | 终端窗口保持打开，用户可以看到命令执行过程 |

### 2.4 多终端管理

| 需求 | 说明 |
|------|------|
| 数量限制 | 默认最多 5 个终端（可配置） |
| 驱逐策略 | 新建时自动关闭最久未使用的（按最后活跃时间） |
| 懒处理 | 不主动检测用户手动关闭，用到时发现不存在则新建 |

### 2.5 终端历史

| 需求 | 说明 |
|------|------|
| 最近 5 个 | 管理最近 5 个输入指令和输出（可配置） |
| 截断 | 输入/输出各截断 200 字符（可配置） |
| 持久化 | 保存到文件，重启后可恢复 |

---

## 三、技术架构

### 3.1 平台抽象设计

```
PlatformTerminal (抽象基类)
├── WindowsTerminal   # Windows 实现
├── MacOSTerminal     # macOS 实现  
└── LinuxTerminal    # Linux 实现
```

### 3.2 跨平台实现方案

| 平台 | 打开终端 | 命令执行 |
|------|----------|----------|
| Windows | `start cmd /K` | subprocess.run + capture_output |
| macOS | osascript Terminal.app | bash -c + capture_output |
| Linux | gnome-terminal/konsole | bash -c + capture_output |

## 四、配置

### 4.1 配置项

| 配置 | 默认值 | 说明 |
|------|--------|------|
| max_terminals | 5 | 最大终端数 |
| history_count | 5 | 历史记录数 |
| history_truncate | 200 | 截断字符数 |
| storage_dir | data/terminals | 存储目录 |

---

## 五、使用流程

### 5.1 Bot 启动时
1. 创建 TerminalManager 实例
2. 注册 ShellTool（传入 terminal_manager）
3. 注册 TerminalTool

### 5.2 执行 shell 命令时(tool的实现, llm无直接感知)
1. 检查 terminal_manager 是否存在
2. 若无默认终端，调用 open_terminal() 自动创建
3. 在终端中执行命令
4. 返回结果

### 5.3 手动管理终端
1. 用户/模型调用 terminal_manager 工具
2. 执行 open/close/list/select 操作
3. 状态持久化到 JSON 文件

---

## 六、已知问题与限制

### 6.1 交互式命令
- SSH 等需要密码输入的命令无法工作
- 需要使用 sshpass 或密钥认证

### 6.2 用户手动关闭
- 懒处理：用到时发现不存在则新建
- 模型无感知，由终端管理器控制

### 6.3 跨平台测试
- Windows
- macOS/Linux

---

## 七、验收标准

1. ✅ Windows 上 ShellTool 自动检测并使用 Git Bash（若存在）
2. ✅ 终端管理工具可以打开/关闭/查看/选择终端
3. ✅ 打开终端时用户能看到一个可见的终端窗口
4. ✅ 终端数量限制为 5 个，超过自动关闭最久未使用的
5. ✅ 终端状态和历史记录持久化到 JSON 文件
6. ✅ 重启后可以恢复终端状态和历史
7. ✅ 跨平台支持（Windows/macOS/Linux）