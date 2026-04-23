# Bot 示例项目指南

> 本文档详细讲解 ModexAgent 的 `examples/bot_project/` 示例的架构、配置和运行方式。

---

## 1. 项目结构

```
examples/bot_project/
├── config/
│   ├── bot_config.yml       # 主配置文件（QQ、LLM、Agent、Memory、MCP、多 Agent、插件）
│   └── mcp.json             # MCP 服务器配置
├── data/
│   ├── memory/              # 记忆持久化存储目录
│   ├── inbox/               # Inbox 消息存储目录
├── skills/
│   ├── main/                # 主 Agent 的 Skills
│   │   ├── weather/SKILL.md
│   │   ├── github/SKILL.md
│   │   ├── memory/SKILL.md
│   │   ├── summarize/SKILL.md
│   │   └── ...
│   └── peers/               # Peer Agent 的 Skills
│       ├── docx/SKILL.md    # Word 文档处理
│       ├── pdf/SKILL.md     # PDF 处理
│       ├── pptx/SKILL.md    # PPT 处理
│       └── xlsx/SKILL.md    # Excel 处理
├── plugins/                 # 插件目录
├── utils/
│   └── config_loader.py     # YAML/JSON 配置加载工具
├── bot_service.py           # ★ Bot 服务主入口
└── qq_adapters.py           # ★ QQ 适配器（Input/Output/Emitter）
```

---

## 2. 启动流程

### 2.1 BotService 初始化

```
BotService.__init__()
  ├── 加载 config/bot_config.yml
  ├── 创建 QQInputAdapter (app_id, secret)
  ├── 创建 QQOutputAdapter (绑定 input_adapter)
  └── 创建 emitter_factory (session_id -> QQBotEmitter)

BotService.initialize()
  │
  ├── 1. 加载配置
  ├── 2. 创建 InMemoryMessageBroker（跨 Agent 消息传递）
  ├── 3. 创建 InMemoryToolManager（支持并行执行）
  │   ├── 注册文件工具：read_file, write_file, edit_file, list_dir
  │   ├── 注册 Shell 工具：shell
  │   └── 注册 MCP 工具（通过 MCPClientManager）
  ├── 4. 创建 LiteLLMProvider（支持 100+ 模型）
  ├── 5. 创建 MemorySystem + MemorySystemContextManager
  │   └── 四层记忆：Working → ShortTerm → History → LongTerm
  ├── 6. 创建 DreamEngine（后台离线长期记忆整理）
  ├── 7. 创建 SkillManager（加载 skills/main/ 下的 SKILL.md）
  ├── 8. 创建 AgentFactory + SubagentManager（多 Agent 协作）
  ├── 9. 创建 AgentPool（常驻 Peer Agent）
  ├── 10. 注册多 Agent 工具：spawn_subagent, send_message
  ├── 11. 加载插件（如 mem0_memory）
  ├── 12. 创建 ReActAgent
  └── 13. 创建 AgentPipeline（组装所有组件）

BotService.start()
  └── pipeline.run()
       ├── input_adapter.start() → 连接 QQ Bot (botpy SDK)
       └── async for input_msg in input_adapter.receive():
              await _process_message(input_msg)
```

### 2.2 消息处理流程

```
QQ 消息到达
    │
    ▼
QQInputAdapter._on_message()
    ├── 消息去重（deque 缓存 1000 条 ID）
    ├── 用户权限过滤（allow_from 配置）
    ├── 提取消息内容和 metadata
    └── 放入 asyncio.Queue
    │
    ▼
AgentPipeline._process_message()
    ├── 内容清洗 (ContentSanitizer)
    ├── session 级别锁（防止并发）
    ├── 加载上下文 + 系统提示词构建
    ├── 增量保存用户消息
    │
    ▼
ReActAgent.run()
    ├── ReAct 循环（最多 20 次迭代）
    ├── LLM 调用（流式/非流式）
    ├── 工具执行（文件操作、Shell、MCP、子 Agent）
    └── 返回 AgentResult
    │
    ▼
QQBotEmitter
    ├── 推理内容 → 只记日志，不发用户
    ├── 工具调用 → 记录到日志，不发给用户
    ├── 最终内容 → 伪流式缓冲 → QQOutputAdapter.send()
    │
    ▼
QQOutputAdapter.send()
    ├── 内容过滤（ThinkTag + Whitespace）
    ├── 群聊消息 → post_group_message()
    └── C2C 私聊 → post_c2c_message()
```

---

## 3. QQ 适配器详解

### 3.1 QQInputAdapter

```python
class QQInputAdapter(InputAdapter):
    def __init__(self, app_id, secret, sandbox=False, allow_from=None):
        self.app_id = app_id
        self.secret = secret
        self._message_queue = asyncio.Queue()
        self._processed_ids = deque(maxlen=1000)  # 消息去重
```

关键特性：
- 使用 `qq-botpy` SDK 接收 QQ C2C 私聊和群聊消息
- 自动重连（5 秒间隔）
- 消息去重防止重复处理
- 支持用户白名单过滤

### 3.2 QQOutputAdapter

```python
class QQOutputAdapter(OutputAdapter):
    supports_streaming = False  # QQ 不支持真流式
    content_filter = ChainedContentFilter([ThinkTagFilter(), WhitespaceFilter()])
```

关键特性：
- **伪流式**：`send_delta()` 缓冲内容，`flush_deltas()` 合并后一次性发送
- 自动路由：根据 `last_input_metadata` 判断 C2C 私聊或群聊
- 内容过滤：移除 think 标签和多余空白

### 3.3 QQBotEmitter

```python
class QQBotEmitter(StreamingAwareEmitter[ReActEvent]):
    async def _on_event(self, event, data):
        if event_name == "model_reasoning":
            logger.info(f"[Reasoning] {data}")   # 只记日志
        elif event_name == "tool_call_start":
            logger.info(f"[Tool Call] {data}")    # 只记日志
        # 基类处理 content 缓冲和 flush
```

---

## 4. 配置详解 (`config/bot_config.yml`)

### 4.1 路径配置

```yaml
paths:
  data_dir: "data"
  memory_dir: "data/memory"
  inbox_dir: "data/inbox"
  skills_dir: "skills/main"
  plugins_dir: "plugins"
```

### 4.2 QQ Bot 配置

```yaml
qq:
  app_id: "${QQ_APP_ID}"        # 从环境变量读取
  secret: "${QQ_SECRET}"
  sandbox: false
  allow_from: ["*"]             # 允许所有用户，或指定用户 ID 列表
```

### 4.3 LLM 配置

```yaml
llm:
  api_key: "${LLM_API_KEY}"
  base_url: "${LLM_BASE_URL:-https://api.minimaxi.com/v1}"
  model: "${LLM_MODEL:-openai/MiniMax-M2.5}"
  temperature: 0.7
  max_tokens: 80000              # 大文件操作需增大此值
```

### 4.4 Agent 配置

```yaml
agent:
  system_prompt: |
    你是一个 AI 助手。
    ## 交互规范
    - 回复使用中文，风格自然、简洁
    - 优先给出直接答案，再补充解释
    ...
  max_iterations: 20
```

### 4.5 多 Agent 配置

```yaml
multi_agent:
  enabled: true
  parent_agent_name: "main"
  allowed_callers: null

  # 同步子 Agent（快速任务）
  subagent_sync:
    enabled: true
    name: "helper-sync"
    system_prompt: |
      你是一个快速执行 Agent...
    max_iterations: 10
    tools:
      file_tools: { enabled: true }
      shell_tools: { enabled: true }

  # Peer Agent（常驻后台 Agent）
  peers:
    - name: "office-expert"
      role: "document"
      capabilities: ["document", "office"]
      system_prompt: |
        你是文档专家 Agent...
      memory:
        enabled: true
        short_term:
          max_messages: 50
          budget_ratio: 0.5
      context_strategy: "persistent"
      skill_dirs:
        - "skills/peers/docx"
        - "skills/peers/pdf"
```

### 4.6 记忆配置

```yaml
memory:
  short_term:
    max_messages: 50
    budget_ratio: 0.5  # 短期记忆 token 上限 = llm.max_tokens * 0.5
```

### 4.7 插件配置

```yaml
plugins:
  enabled: true
  configurations:
    tool_call_cleanup:
      enabled: true
    mem0_memory:
      enabled: false
      workspace: "./data/vector_memory"
      vector_store: "chroma"
```

### 4.8 MCP 配置

```yaml
mcp:
  enabled: true
  config_file: "mcp.json"
```

### 4.9 工具配置

```yaml
tools:
  file_tools:
    enabled: true
    allowed_directories: ["."]
  shell_tools:
    enabled: true
    timeout: 60
    restrict_to_workspace: true
  mcp_tools:
    enabled: true
    server_filter:
      - "fetch"
      - "mcp-deepwiki"
```

---

## 5. 多 Agent 协作

### 5.1 Subagent（临时子 Agent）

**同步调用** (`spawn_subagent`)：
- 主 Agent 阻塞等待子 Agent 完成
- 适用于快速、简单的任务
- 结果立即返回

**异步调用** (`spawn_subagent_async`)：
- 启动子 Agent 后立即返回
- 结果通过 Inbox 异步传递
- 适用于复杂、耗时较长的任务

### 5.2 Peer Agent（常驻 Agent）

Peer Agent 是长期运行的后台 Agent，通过 `AgentPool` 管理：

```yaml
peers:
  - name: "office-expert"
    role: "document"
    capabilities: ["document", "office"]
    context_strategy: "persistent"  # 持久化上下文
    memory:
      enabled: true                 # 独立记忆系统
```

**关键特性**：
- 独立的生命周期管理
- 独立的记忆系统（使用 `PeerPairScope` 隔离）
- 通过 `send_message_async` 与主 Agent 通信

**通信规则**：
```
Peer Agent 完成任务后，必须通过 send_message_async 发送结果：

send_message_async(
    target_agent="main",
    content="任务执行摘要：...\n关键结果：..."
)
```

**注意**：如果 Peer Agent 只输出普通文本而不调用 `send_message_async`，主 Agent 将永远收不到结果。

### 5.3 Agent 间消息

通过 `send_message` 和 `send_message_async` 工具：

```python
# 同步发送（等待回复）
send_message(target_agent="office-expert", content="请处理这个文档")

# 异步发送（不等待回复）
send_message_async(target_agent="office-expert", content="请后台处理")
```

---

## 6. 插件系统

### 6.1 内置插件

**ToolCallCleanupPlugin**：
- 自动清理工具调用中的多余空白
- 提高工具调用成功率

**Mem0MemoryProvider**（可选）：
- 向量检索增强的记忆系统
- 需要安装：`pip install mem0ai chromadb sentence-transformers`

### 6.2 配置插件

```yaml
plugins:
  enabled: true
  configurations:
    mem0_memory:
      enabled: true
      workspace: "./data/vector_memory"
      vector_store: "chroma"
      embedding_provider: "openai"
      embedding_model: "text-embedding-3-small"
```

---

## 7. 运行方式

### 7.1 环境准备

```bash
# 1. 安装依赖
pip install -e ".[dev,llm,storage]"

# 2. 配置环境变量
cp .env.example .env
# 编辑 .env 文件，填写：
# - QQ_APP_ID
# - QQ_SECRET
# - LLM_API_KEY
```

### 7.2 启动服务

```bash
# 进入示例目录
cd examples/bot_project

# 启动 Bot 服务
python bot_service.py
```

### 7.3 使用 Docker（可选）

```bash
# 构建镜像
docker build -t ModexAgent-bot .

# 运行容器
docker run -d \
  --env-file .env \
  -v $(pwd)/data:/app/data \
  ModexAgent-bot
```

---

## 8. 日志系统

Bot 服务配置了完善的日志系统：

- **控制台输出**：INFO 及以上级别
- **文件日志**：DEBUG 及以上级别，写入 `logs/bot.log`
- **日志轮转**：50MB 单文件，保留 10 个备份
- **第三方库抑制**：asyncio、LiteLLM、botpy、mcp 等库日志设为 WARNING

### 8.1 日志配置

```python
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.StreamHandler(),
        RotatingFileHandler("logs/bot.log", maxBytes=50*1024*1024, backupCount=10),
    ],
)
```

---

## 9. 扩展到其他平台

BotService 被设计为平台无关的，只需替换 InputAdapter 和 OutputAdapter 即可接入其他平台：

```python
# 例如：Telegram
service = BotService(
    config_dir=config_dir,
    input_adapter=TelegramInputAdapter(token="..."),
    output_adapter=TelegramOutputAdapter(...),
    emitter_factory=lambda sid: TelegramEmitter(...),
)
```

详见 [扩展开发指南](./extension-guide.md)。

---

## 10. 最佳实践

### 10.1 配置管理

- 使用环境变量存储敏感信息（API Key、Secret）
- 使用 `.env` 文件管理本地开发配置
- 生产环境使用密钥管理服务

### 10.2 记忆优化

- 根据对话长度调整 `max_messages` 和 `budget_ratio`
- 大文件操作前增大 `llm.max_tokens`
- 启用 `mem0_memory` 插件增强长期记忆

### 10.3 多 Agent 协作

- 合理划分 Agent 职责
- 使用 `PeerPairScope` 隔离不同 Agent 对的记忆
- 异步任务使用 `send_message_async` 回传结果

### 10.4 安全建议

- 限制 `allowed_directories` 防止文件越界访问
- 启用 `restrict_to_workspace` 限制 Shell 工具
- 配置 `allow_from` 限制可访问的用户
