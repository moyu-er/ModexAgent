# OpenAI Provider 设计文档

**日期:** 2026-05-13
**版本:** 1.0
**状态:** 待评审

---

## 1. 动机

当前 `LiteLLMProvider` 是唯一的 LLM Provider 实现。需要新增一个专用的 `OpenAIProvider`：

- 使用 `openai` 官方 SDK（版本与 litellm 1.82.6 依赖的 openai 2.24.0 一致）
- 支持 OpenAI 特有的配置项（base_url、extra_headers）
- 成为 `examples/bot_project/` 的默认 provider
- 消除 litellm 间接层带来的类型不确定性和 hasattr/dict 探测
- 提供统一的中间类型，后续可复用到 LiteLLMProvider 及其他 provider

## 2. 设计原则

1. **类型安全优先** — 全程使用 dataclass 和 Pydantic 模型，禁止 `hasattr`/`getattr` 探测和裸 `dict[str, Any]`
2. **组合优于继承** — 共享逻辑通过 `shared/` 子包中的工具类型和函数组合，不引入中间基类
3. **不做消息预处理** — pipeline/governance 层已完成消息清洗，provider 不重复处理
4. **结构化错误** — 优先从 openai SDK 异常对象提取 `status_code`、`error.type`、`error.code`、`Retry-After` 等字段，字符串扫描仅作兜底

## 3. 文件结构

```
framework/providers/
├── __init__.py              # 只导出 LiteLLMProvider + OpenAIProvider
├── litellm_provider.py      # 现有实现，后续轻量重构使用 shared/ 类型
├── openai_provider.py       # 新增 OpenAIProvider(StreamingLLMProvider)
└── shared/                  # 公共中间类型 + 工具函数（不暴露给外部）
    ├── __init__.py
    ├── delta.py             # StreamDelta, ParsedResponse
    └── errors.py            # classify_openai_error()
```

## 4. 共享中间类型 (`shared/`)

### 4.1 `shared/delta.py`

```python
from dataclasses import dataclass, field
from framework.core.tool_call_accumulator import ToolCallChunk
from framework.core.types import ToolCall


def extract_reasoning(model: "BaseModel") -> str | None:
    """从 SDK Pydantic 模型提取 reasoning_content 扩展字段。

    reasoning_content 不在 openai SDK 标准类型字段中，
    直接通过 Pydantic BaseModel.model_extra（公开 API）提取。
    不使用 getattr/hasattr 反射探测。
    """
    extra = model.model_extra
    if extra is None:
        return None
    return extra.get("reasoning_content")


@dataclass
class StreamDelta:
    """从 LLM 流式响应 chunk 提取的结构化片段。

    统一抽象，各 provider 只需实现 from_* 类方法即可适配不同 SDK。
    所有字段类型明确，不使用 dict 或 getattr 探测。
    """
    content: str | None = None
    reasoning_content: str | None = None
    tool_call_chunks: list[ToolCallChunk] = field(default_factory=list)
    finish_reason: str | None = None

    @classmethod
    def from_openai(cls, delta: "ChoiceDelta") -> "StreamDelta":
        """从 openai SDK ChoiceDelta 对象构建。

        所有字段通过属性访问（delta.content, delta.tool_calls 等），
        reasoning_content 通过 extract_reasoning() 从 model_extra 提取。
        """
        ...

@dataclass
class ParsedResponse:
    """非流式 LLM 响应的结构化解析结果。

    作为 LLMResponse 的前置中间载体，统一承载解析逻辑。
    """
    content: str | None
    reasoning_content: str | None
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str = "stop"
    usage: dict[str, int] = field(default_factory=dict)

    @classmethod
    def from_openai(cls, response: "ChatCompletion") -> "ParsedResponse":
        """从 openai SDK ChatCompletion 对象解析。

        Args:
            response: openai.types.chat.ChatCompletion 实例
        """
        ...
```

### 4.2 `shared/errors.py`

```python
from framework.core.llm_error import LLMErrorInfo, LLMErrorKind

def classify_openai_error(exc: Exception) -> LLMErrorInfo:
    """从 openai SDK 异常对象提取结构化错误信息。

    优先级:
    1. 类型匹配：APIStatusError 子类直接映射（RateLimitError → RATE_LIMIT 等）
    2. 结构提取：status_code、error.type、error.code、Retry-After header
    3. 字符串扫描：兜底匹配 transient_markers
    """
    ...
```

## 5. OpenAIProvider 设计

### 5.1 构造

```python
class OpenAIProvider(StreamingLLMProvider):
    def __init__(
        self,
        model: str,
        api_key: str | None = None,
        base_url: str | None = None,
        temperature: float = 0.7,
        max_tokens: int | None = None,
        timeout: float = 45.0,
        stream_idle_timeout: float = 90.0,
        parse_think_tags: bool = False,
        reasoning_effort: str | None = None,
        extra_headers: dict[str, str] | None = None,
        safety: RuntimeSafetyPolicy | None = None,
    ):
```

内部创建 `openai.AsyncOpenAI` 客户端：
- `api_key` 缺省值为 `"not-needed"`（兼容本地代理）
- `base_url` 映射到 `AsyncOpenAI(base_url=...)`
- `extra_headers` 映射到 `AsyncOpenAI(default_headers=...)`
- `max_retries=0`，重试由框架 `StreamingLLMProvider._execute_with_retry()` 管理

### 5.2 非流式调用 (`chat()`)

```
AsyncOpenAI.chat.completions.create(
    model=...,
    messages=messages,           # list[dict] → SDK 接受 dict
    temperature=...,
    max_tokens=...,
    tools=tools,                 # [{"type":"function","function":{...}}]  框架标准格式
    reasoning_effort=...,        # Optional[ReasoningEffort] 直接参数
    extra_headers=...,           # 单次调用自定义 header
    stream=False,
)
  → ChatCompletion
    → ParsedResponse.from_openai(response)
      ├── choices[0].message.content             → content
      ├── choices[0].message.tool_calls           → 遍历: ToolCall(
      │     name=tc.function.name,
      │     arguments=json.loads(tc.function.arguments),  # SDK 返回 JSON str
      │     call_id=tc.id,
      │   )
      ├── extract_reasoning(msg)                   → reasoning_content
      │     (Pydantic model_extra 公开 API，非 getattr 探测)
      ├── choices[0].finish_reason                → finish_reason
      └── response.usage                          → dict(prompt_tokens, completion_tokens, total_tokens)
    → LLMResponse(...)
```

异常路径：捕获 openai SDK 异常 → `classify_openai_error(exc)` → `LLMResponse(finish_reason="error", error_info=...)`

### 5.3 流式调用 (`chat_stream()`)

```
AsyncOpenAI.chat.completions.create(
    model=...,
    messages=messages,
    temperature=...,
    max_tokens=...,
    tools=tools,
    reasoning_effort=...,
    extra_headers=...,
    stream=True,
    stream_options={"include_usage": True},    # 获取 usage 统计
)
  → async for ChatCompletionChunk
    ├── if not chunk.choices: continue           # 空 choices 防护
    → StreamDelta.from_openai(chunk.choices[0].delta)
      ├── delta.content                             → 累积, on_content_delta() 回调
      ├── extract_reasoning(delta)                   → 累积, on_reasoning_delta() 回调
      ├── delta.tool_calls → ToolCallChunk(...)     → ToolCallAccumulator.add_chunk()
    ├── chunk.choices[0].finish_reason              → 最终 finish_reason
    └── chunk.usage (最后一个 chunk)                  → usage
  → 聚合: "".join(content_parts), completed_tool_calls, reasoning, finish_reason, usage
  → LLMResponse(...)
```

`stream_idle_timeout` 保护：使用 `asyncio.wait_for(anext(iterator), timeout=...)`，与 LiteLLMProvider 一致。

ThinkTag 提取：与 LiteLLMProvider 一致，`ThinkTagExtractor.feed()` 处理无原生 reasoning 的场景。

## 6. 错误分类映射

| openai SDK 异常类 | LLMErrorKind | should_retry |
|---|---|---|
| `RateLimitError` | RATE_LIMIT | True |
| `APITimeoutError` | TIMEOUT | True |
| `APIConnectionError` | CONNECTION | True |
| `InternalServerError` | SERVER | True |
| `AuthenticationError` | AUTH | False |
| `PermissionDeniedError` | AUTH | False |
| `BadRequestError` | INVALID_REQUEST | False |

RateLimitError 细分：
- `error.type == "insufficient_quota"` → QUOTA, should_retry=False
- `error.type == "rate_limit_exceeded"` → RATE_LIMIT, should_retry=True

## 7. 数据流总览

```
                    ┌──────────────────────────┐
                    │   StreamingLLMProvider    │
                    │   (core/provider.py)      │
                    │   _execute_with_retry()   │
                    └──────────┬───────────────┘
                               │
              ┌────────────────┼────────────────┐
              │                │                │
     LiteLLMProvider    OpenAIProvider     FutureProvider
              │                │                │
              │    shared/delta.py              │
              │    ├─ StreamDelta               │
              │    └─ ParsedResponse            │
              │                                 │
              │    shared/errors.py             │
              │    └─ classify_openai_error()   │
              │                                 │
              └────────────┬────────────────────┘
                           │
                    LLMResponse
                    (framework.core.types)
```

## 8. 需要新增/修改的文件

### 新增文件

| 文件 | 说明 |
|------|------|
| `framework/providers/shared/__init__.py` | 包声明 |
| `framework/providers/shared/delta.py` | StreamDelta, ParsedResponse |
| `framework/providers/shared/errors.py` | classify_openai_error() |
| `framework/providers/openai_provider.py` | OpenAIProvider |
| `tests/unit/providers/test_openai_provider.py` | 单元测试 |
| `tests/unit/providers/test_shared_delta.py` | StreamDelta/ParsedResponse 测试 |
| `tests/unit/providers/test_shared_errors.py` | 错误分类测试 |

### 修改文件

| 文件 | 变更 |
|------|------|
| `framework/providers/__init__.py` | 新增 OpenAIProvider 导出 |

### 不修改文件

- `framework/providers/litellm_provider.py` — 本次不动，后续重构时可改为使用 `shared/` 类型
- `framework/core/provider.py` — ABCs 不变
- `framework/core/tool_call_accumulator.py` — 继续复用

## 9. 测试策略

### 单元测试

- **StreamDelta.from_openai()** — mock openai SDK ChoiceDelta 对象，验证各字段提取正确
- **ParsedResponse.from_openai()** — mock ChatCompletion 对象，验证 content/tool_calls/reasoning/usage 解析
- **classify_openai_error()** — 覆盖所有 SDK 异常类型，验证 LLMErrorInfo 映射
- **OpenAIProvider.chat()** — mock AsyncOpenAI.client.chat.completions.create，验证请求参数构建和响应解析
- **OpenAIProvider.chat_stream()** — mock stream 迭代器，验证流式累积和回调调用
- **stream_idle_timeout** — 验证超时保护正确终止并返回 partial content
- **ThinkTag 提取** — 验证无原生 reasoning 时的 tag 剥离回退

### 集成测试

- 可选：使用真实 API key 调 OpenAI API，验证端到端

## 10. 与 bot_project 的集成计划

后续将 `bot_service.py` 中的 `LiteLLMProvider` 替换为 `OpenAIProvider`：

```python
# 当前
provider = LiteLLMProvider(model=..., api_key=..., base_url=...)

# 改为
provider = OpenAIProvider(
    model=config.llm.model,
    api_key=config.llm.api_key,
    base_url=config.llm.base_url,
    extra_headers=config.llm.extra_headers,
    ...
)
```

IOC 配置层 `LLMProviderConfig` 已包含 `extra_headers` 和 `reasoning_effort` 字段，直接映射即可。

### 参数传递路径

```
LLMProviderConfig (IOC)
  ├── model, api_key, base_url, temperature, max_tokens
  ├── extra_headers  → AsyncOpenAI(default_headers=...)  客户端级 header
  │                   + chat(extra_headers=...)            单次调用级 header
  ├── reasoning_effort → chat(reasoning_effort=...)       SDK 直接参数(非 extra_body)
  ├── timeout         → httpx.Timeout(timeout)            HTTP 超时
  └── parse_think_tags → ThinkTagExtractor                reasoning 回退提取

BotService → create_llm_provider(config) → OpenAIProvider → AsyncOpenAI
```

### 与 OpenAI 代理/网关的兼容

通过 `base_url` 和 `extra_headers` 的组合，支持任意 OpenAI 兼容网关：
- **直连 OpenAI**: `base_url=None`, `api_key=sk-...`
- **代理**: `base_url="https://proxy.example.com/v1"`, `api_key=...`
- **网关含自定义 header**: `extra_headers={"X-Custom-Auth": "..."}`
- **本地模型**: `base_url="http://localhost:8080/v1"`, `api_key="not-needed"`

参数中保留 `**kwargs` 透传给 `chat.completions.create()`，支持 `extra_body` 等扩展。
