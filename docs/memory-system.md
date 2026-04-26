# 三层记忆系统

> 本文档详细讲解 ModexAgent 的多层记忆架构。

---

## 1. 架构概览

```
┌─────────────────────────────────────────────────────────────────┐
│                        MemorySystem                             │
│                      （统一入口）                                │
├──────────────────┬─────────────┬────────────────────────────────┤
│   Short-Term     │   History   │         Long-Term              │
│    Memory        │   Archive   │          Memory                │
│   (近期对话)      │  (历史摘要)  │      (用户画像/知识)            │
├──────────────────┼─────────────┼────────────────────────────────┤
│   SessionScope   │  UserScope  │        UserScope               │
│   文件存储        │  文件存储    │       文件存储                  │
│   最近 N 条      │  压缩归档    │       SOUL.md                  │
│   自动压缩        │  Dream整理  │       USER.md                  │
│   cursor/delete  │             │       MEMORY.md                │
└──────────────────┴─────────────┴────────────────────────────────┘
                              │
                              ▼
                    ┌─────────────────────┐
                    │  ContentTransformer │
                    │  (Base64Sanitize)   │
                    └─────────────────────┘
```

---

## 2. 三层记忆详解

### 2.1 Short-Term Memory — 短期记忆

**作用**：保存最近的多轮对话历史，供 LLM 上下文使用。

| 属性 | 说明 |
|------|------|
| 存储 | 文件（FileStorage）或内存 |
| 作用域 | SessionScope（每个会话独立） |
| 容量 | 可配置（默认 100 条消息 / token 预算） |
| 压缩 | 自动 LLM 压缩 + 语义归档 |
| 内容转换 | 支持 ContentTransformer（如 Base64SanitizeTransformer） |

**配置**：

```python
from framework.memory.layers import MemoryLayerConfigSet, SessionMemoryConfig
from framework.memory.consolidation.consolidator import Consolidator
from framework.memory.archive import SemanticArchiveStrategy
from framework.memory.content_transform import Base64SanitizeTransformer

# 通过 MemoryLayerConfigSet 配置各层
layers = MemoryLayerConfigSet(
    session=SessionMemoryConfig(max_messages=100),
)
# 压缩策略由 DefaultMemoryCompressionCoordinator 统一管理
# 如需自定义，在创建 memory system 时传入 coordinator 参数
```

**压缩策略**：

1. **Consolidator** - 使用 LLM 对旧消息进行智能摘要
2. **ToolChainCompression** - 工具链感知压缩，确保 tool_calls 和 tool 结果成对保留
3. **ImportanceCompression** - 基于重要性评分选择性保留消息
4. **TokenWindowCompression** - 基于 token 窗口的简单截断
5. **HybridCompression** - 组合多种策略

### 2.2 History Archive — 历史归档

**作用**：保存跨会话的长期对话摘要，按用户维度存储。

| 属性 | 说明 |
|------|------|
| 存储 | 文件（FileStorage） |
| 作用域 | UserScope（同一用户跨会话共享） |
| 格式 | JSON Lines（history.jsonl） |
| 整理 | DreamEngine 定期离线整理 |

**DreamEngine**：后台离线进程，定期扫描 History，提取关键信息更新到 Long-Term Memory。

### 2.3 Long-Term Memory — 长期记忆

**作用**：结构化存储用户画像、知识、偏好。

| 属性 | 说明 |
|------|------|
| 存储 | Markdown 文件 |
| 作用域 | UserScope |
| 文件 | `SOUL.md`（Agent 自我认知）、`USER.md`（用户画像） |
| 更新 | DreamEngine 自动更新 |

**SOUL.md 示例**：

```markdown
# Agent 自我认知

## 核心能力
- 代码编写与调试
- 文件操作与项目管理
- 多 Agent 任务协调

## 工作风格
- 优先给出直接答案
- 代码块使用正确格式
- 不确定时如实说明
```

**USER.md 示例**：

```markdown
# 用户画像

## 基本信息
- 偏好语言：中文
- 技术栈：Python, TypeScript

## 交互习惯
- 喜欢简洁直接的回答
- 经常询问代码相关问题
```

---

## 3. Scope 体系

记忆的分组维度：

| Scope | 说明 | 示例 |
|-------|------|------|
| `SessionScope` | 会话级别 | `session_123` |
| `UserScope` | 用户级别 | `user_123` |
| `TenantScope` | 租户/组织级别 | `org_acme` |
| `AgentScope` | Agent 类型级别 | `agent_main` |
| `ChannelScope` | 频道级别 | `channel_general` |
| `ChatScope` | 聊天群组级别 | `qq_group_123` |
| `PeerPairScope` | 点对点通信级别 | `conv_id:sender:receiver` |
| `GlobalScope` | 全局共享 | `global` |
| `CompositeScope` | 组合多个维度 | `tenant:user:session` |

```python
from framework.memory.core.scope import (
    SessionScope, UserScope, TenantScope, AgentScope,
    ChannelScope, ChatScope, PeerPairScope, CompositeScope, GlobalScope,
    MemoryContext
)

# 单一会话
session_scope = SessionScope()

# 单一用户
user_scope = UserScope()

# 点对点通信（多 Agent 场景）
peer_scope = PeerPairScope()
context = MemoryContext(
    session_id="conv_123",
    sender_agent="agent_a",
    receiver_agent="agent_b"
)
key = peer_scope.get_scope_key(context)  # "conv_123:agent_a:agent_b"

# 组合
composite = CompositeScope(TenantScope(), UserScope(), SessionScope())
```

---

## 4. ContentTransformer — 内容转换

用于在存储前转换消息内容，主要用于处理多媒体内容。

### 4.1 Base64SanitizeTransformer

框架内置的唯一 transformer，将 base64 编码的多媒体内容替换为文本占位符。

```python
from framework.memory.content_transform import Base64SanitizeTransformer

# 创建转换器
transformer = Base64SanitizeTransformer(
    placeholder_template="[media: {name}]"
)

# 配置到短期记忆
config = ShortTermConfig(
    content_transformer=transformer,
)
```

**支持的 block 类型**：
- `image_url` with data: URI → 替换为占位符
- `image_url` with http(s) URL → 保留原样
- `input_audio` with data: URI → 替换为占位符
- `file` with data: URI → 替换为占位符

**转换示例**：
```python
# 转换前
{
    "role": "user",
    "content": [
        {"type": "text", "text": "请分析这张图片"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,iVBORw0..."}}
    ]
}

# 转换后
{
    "role": "user",
    "content": "请分析这张图片\n[media: image.png]",
    "metadata": {
        "media_info": [
            {"type": "image", "path": "/path/to/image.png", "mime": "image/png", "placeholder": "[media: image.png]"}
        ]
    }
}
```

---

## 5. MemorySystem 使用

### 5.1 初始化

```python
from framework.memory.system import create_memory_system

memory_system = create_memory_system(
    workspace=Path("./data/memory"),
    llm_provider=provider,
)
await memory_system.initialize()
```

### 5.2 默认配置

**单用户桌面场景**：
```python
# 单用户桌面场景默认配置
memory_system = create_memory_system(
    workspace=Path("./memory"),
    llm_provider=provider,
    config={
        "session": {"max_messages": 100},
        "auto_compact": {"max_messages": 100, "max_tokens": 8000},
    },
)
await memory_system.initialize()
```

**多租户 SaaS 场景**：
```python
# 多租户 SaaS 场景默认配置
from framework.memory.layers import MemoryLayerConfigSet
from framework.memory.core.scope import TenantScope, UserScope, SessionScope, CompositeScope
from framework.memory.layers.config import SessionMemoryConfig, ArchiveMemoryConfig, KnowledgeMemoryConfig

layers = MemoryLayerConfigSet(
    session=SessionMemoryConfig(scope=CompositeScope(TenantScope(), UserScope(), SessionScope())),
    archive=ArchiveMemoryConfig(scope=CompositeScope(TenantScope(), UserScope())),
    knowledge=KnowledgeMemoryConfig(scope=CompositeScope(TenantScope(), UserScope())),
)
memory_system = create_memory_system(workspace=Path("./memory"), layer_config=layers)
await memory_system.initialize()
```

### 5.3 基本操作

```python
from framework.memory.core.scope import MemoryContext

context = MemoryContext(
    session_id="session_123",
    user_id="user_456",
)

# 添加消息到短期记忆
await memory_system.add_message(context, {"role": "user", "content": "Hello"})

# 批量添加消息
await memory_system.add_messages(context, messages)

# 获取历史
history = await memory_system.get_history(context, max_messages=50)

# 获取压缩摘要
summary = await memory_system.get_compression_summary(context)

# 获取历史归档条目
entries = await memory_system.get_history_entries(context, limit=5)

# 获取长期记忆
long_term = await memory_system.get_long_term(context)
```

### 5.4 Working Memory（已移除）

> **注意**：Working Memory 层已在 v0.3.0+ 中移除（P11 重构）。
> 所有消息直接写入 Short-Term Memory，由 `compression_mode`（`cursor` / `delete`）控制可见性。
> `cursor` 模式下，消息物理保留但前 `.compression_cursor` 条对 LLM 不可见；`delete` 模式下物理删除。

### 5.5 与 ContextManager 集成

```python
from framework.memory.system import MemorySystemContextManager

context_manager = MemorySystemContextManager(
    memory_system=memory_system,
    base_system_prompt="You are a helpful assistant.",
)

# 使用方式与普通 ContextManager 相同
```

---

## 6. 压缩策略详解

### 6.1 ToolChainCompression

工具链感知压缩，确保 tool_calls 和对应的 tool 结果被同时保留或移除。

```python
from framework.memory.compression.tool_chain import ToolChainCompression

strategy = ToolChainCompression(
    protected_count=2,  # 保护头部消息
    min_tail_keep=1,    # 尾部至少保留消息数
)
```

### 6.2 ImportanceCompression

基于重要性评分选择性压缩消息。

```python
from framework.memory.compression.importance import ImportanceCompression
from framework.memory.compression.importance import HeuristicImportanceScorer

scorer = HeuristicImportanceScorer()
strategy = ImportanceCompression(
    importance_scorer=scorer,
    protected_count=2,
    min_tail_keep=1,
)
```

**评分规则**（0.0 ~ 1.0）：
- system 消息: 1.0 (最高优先级)
- assistant 的 tool_calls 消息: 0.9 (工具调用链条关键)
- user 消息: 基础 0.6，包含问号或长度较长时加分，最高 0.85
- tool 结果消息: 0.5
- 极短的无意义消息 (如 "ok", "thanks"): 0.2

### 6.3 TokenWindowCompression

基于 token 窗口的简单截断策略。

```python
from framework.memory.compression.token_window import TokenWindowCompression

strategy = TokenWindowCompression(
    protected_count=2,
    min_tail_keep=1,
)
```

### 6.4 HybridCompression

组合多种压缩策略。

```python
from framework.memory.compression.hybrid import HybridCompression

strategy = HybridCompression([
    ToolChainCompression(protected_count=2, min_tail_keep=1),
    ImportanceCompression(scorer=scorer, protected_count=2, min_tail_keep=1),
])
```

---

## 7. 插件系统集成

MemorySystem 支持通过 MemoryProvider 插件扩展功能。

```python
from framework.plugins import MemoryProvider

class CustomMemoryProvider(MemoryProvider):
    @property
    def name(self) -> str:
        return "custom_provider"
    
    async def add(self, messages, context):
        # 处理新增消息
        pass
    
    async def search(self, query, context, limit=5):
        # 搜索记忆
        return []
    
    async def prefetch(self, query, context):
        # 预取相关记忆
        return None
    
    async def on_pre_compress(self, messages, context):
        # 压缩前回调
        pass
    
    async def shutdown(self):
        # 关闭资源
        pass

# 注册 provider
memory_system.add_provider(CustomMemoryProvider())

# 搜索所有 provider
results = await memory_system.search_memories("query", context, limit=5)

# 预取记忆
prefetch = await memory_system.prefetch_memories("query", context)
```

---

## 8. 最佳实践

### 8.1 单用户桌面应用

```python
memory_system = MemorySystem(
    workspace=Path("~/.myapp/memory"),
    layers=MemorySystem.default_single_user_layers(
        workspace=Path("~/.myapp/memory"),
        llm_provider=llm_provider,
        auto_llm_compression=True,
        llm_max_tokens=128000,
        budget_ratio=0.5,
    )
)
```

### 8.2 多 Agent 协作场景

```python
# 使用 PeerPairScope 隔离不同 Agent 对之间的记忆
from framework.memory.core.scope import PeerPairScope

# Peer Agent 通常只需要单层 short_term 记忆
layers = {
    "short_term": LayerConfig(
        scope=PeerPairScope(),
        storage=FileStorage(workspace),
        compression_strategy=Consolidator(llm_provider=provider),
    ),
}

memory_system = MemorySystem(workspace=workspace, layers=layers)
```

### 8.3 多媒体内容处理

```python
# 启用 Base64SanitizeTransformer 处理多媒体内容
from framework.memory.content_transform import Base64SanitizeTransformer

config = ShortTermConfig(
    max_messages=100,
    max_tokens=8000,
    content_transformer=Base64SanitizeTransformer(),
)
```
