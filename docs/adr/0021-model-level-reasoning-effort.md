# Model-level reasoning effort configuration

Status: proposed (2026-07-12)

## Context

Reasoning-capable models (OpenAI gpt-5.x/o-series, and others via OpenAI-compatible APIs) expose a `reasoning_effort` parameter that controls how much model work is spent on internal reasoning. The ModexAgent framework already has the plumbing to pass this value: `OpenAIProvider` and `LiteLLMProvider` both accept `reasoning_effort`, and `AgentDescriptor.llm_config` carries a `reasoning_effort` field. However, the framework `LLMConfig` and the bot's `BotModelConfig` do not expose the field, so it is always `None` in practice and never sent to a model.

We want users to be able to choose a reasoning effort per model from the WebUI, persist it in `config/model.yml`, and have it flow into the LLM call. At the same time we must preserve the current behavior when the feature is not used: no `reasoning_effort` parameter is sent, so non-reasoning models and non-OpenAI providers keep working exactly as before.

Key trade-offs:

- **Where to place the config**: per-model is the natural fit because the parameter is model-specific and the bot already stores `temperature`, `max_output_tokens`, and `capabilities` per model in `config/model.yml`.
- **Whether to validate against model capabilities**: some models support only a subset of `reasoning_effort` values. Validating this would require a provider-specific capability matrix that grows stale quickly. We choose to pass the value through and let the provider API reject unsupported values, keeping the config layer simple.
- **What `none` means**: the UI uses a dropdown whose default state is labeled `none`. For backend simplicity, `ReasoningEffort.NONE` and an absent field are treated identically: no `reasoning_effort` parameter is sent. This preserves backward compatibility and avoids the UI needing a special "not configured" option.
- **Type representation**: `reasoning_effort` is represented as a typed `StrEnum` everywhere, not a raw string. This aligns with the project type-safety rules (enums/constants over raw strings) and makes invalid values fail at validation time rather than at runtime.

## Decision

### 1. Define a typed `ReasoningEffort` enum in the framework

`ReasoningEffort` is a `StrEnum` with the six values supported by OpenAI's reasoning API:

```python
class ReasoningEffort(StrEnum):
    NONE = "none"
    MINIMAL = "minimal"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    XHIGH = "xhigh"
```

It lives in the framework's core constants/enums module so that `LLMConfig`, `LLMProviderConfig`, provider implementations, and the bot model config can all reference the same type. No raw strings are used for reasoning effort values anywhere in the codebase.

### 2. Add `reasoning_effort` to `config/model.yml` per model

Each model entry in `config/model.yml` may contain:

```yaml
reasoning_effort: medium
```

Allowed values are exactly the members of `ReasoningEffort`. The field has a default of `ReasoningEffort.NONE`.

### 3. Backend maps `ReasoningEffort.NONE` to "do not send"

Both an absent field and a field set to `none` result in the provider receiving `ReasoningEffort.NONE`. The provider only sends the parameter when the value is not `NONE`:

```python
if reasoning_effort != ReasoningEffort.NONE:
    params["reasoning_effort"] = reasoning_effort.value
```

This mapping is localized to the provider implementation so that the rest of the framework can carry a single `ReasoningEffort` value without special-casing `none`.

### 4. Wire the enum through the framework plumbing

- `LLMConfig` carries `reasoning_effort: ReasoningEffort = ReasoningEffort.NONE`.
- `LLMProviderConfig` carries `reasoning_effort: ReasoningEffort = ReasoningEffort.NONE`.
- `AgentDescriptor.llm_config` carries `reasoning_effort: ReasoningEffort = ReasoningEffort.NONE`.
- `create_llm_provider` passes `reasoning_effort` to both `OpenAIProvider` and `LiteLLMProvider`.
- Both providers accept `reasoning_effort: ReasoningEffort` and apply the `NONE` guard before the API call.

### 5. Frontend exposes a single dropdown per model

The WebUI Models tab adds a dropdown with the six enum values. The default selection is `none`. Changing it writes the corresponding string to `config/model.yml`. No per-turn quick-switch, no IM-side toggle, no `show_reasoning` config — the UI only controls the effort level.

### 6. Reasoning content display and persistence stay unchanged

The reasoning chain produced by a model is already surfaced via `reasoning_content`/`on_reasoning_delta` and rendered in the WebUI by `ReasoningBlock`. `ChatMessage.to_dict()` already strips `reasoning_content` before storage, so reasoning is not persisted to memory. This ADR does not change that behavior.

## Consequences

**Positive:**
- Users can tune reasoning effort per model from the UI without editing YAML.
- Default/absent/`ReasoningEffort.NONE` all preserve today's behavior, so existing deployments are not disrupted.
- Type-safe: `ReasoningEffort` is a single source of truth for allowed values, eliminating raw-string drift and catching typos at validation time.
- The same config works for both native OpenAI SDK and LiteLLM paths.

**Negative:**
- Selecting an unsupported value for a given model will cause the provider API to return an error. The user is responsible for choosing a value compatible with the model.
- `ReasoningEffort.NONE` cannot be used to explicitly request `reasoning_effort: none` from a provider that supports it; if that distinction becomes important later, a separate "send `none`" mode would need to be introduced.
- A framework-wide enum change touches `LLMConfig`, `LLMProviderConfig`, `AgentDescriptor`, both providers, and the bot model config, making this a cross-cutting type change rather than a purely additive field.

**Not in scope:**
- Model-specific capability validation or clamping.
- A `show_reasoning` toggle (always shown) or per-turn reasoning effort override.
- IM adapter controls for reasoning effort.
- Support for provider-specific reasoning modes beyond `reasoning_effort` (e.g., `reasoning.mode` / `reasoning.summary`).
