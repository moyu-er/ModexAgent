# Model-level Reasoning Effort Configuration

Status: proposed

Related: ADR-0021 (`docs/adr/0021-model-level-reasoning-effort.md`); `CONTEXT.md` → "Reasoning Effort", "Reasoning Content".

## Problem Statement

ModexAgent already streams and displays model reasoning content in the WebUI, and the framework providers (`OpenAIProvider`, `LiteLLMProvider`) already accept a `reasoning_effort` parameter. However, there is no typed, user-configurable way to set this value per model. The `reasoning_effort` field exists on `AgentDescriptor.llm_config` but is typed as an optional string and never populated, so every LLM call falls back to the model's default reasoning behavior.

A user running a reasoning-capable model (e.g., OpenAI gpt-5.x) cannot tune how much reasoning the model performs without hand-editing code. Worse, if we expose the field as a raw string, invalid values will only surface at runtime when the provider API rejects them. A typed enum catches these errors at config load time and keeps the codebase consistent with the project's type-safety rules.

The problem is therefore: give users a per-model, persisted, UI-editable `reasoning_effort` setting represented as a typed enum everywhere, while keeping the default behavior byte-for-byte identical to today.

## Solution

Add a typed `ReasoningEffort` enum to the framework and expose it on `LLMConfig`, `LLMProviderConfig`, `AgentDescriptor.llm_config`, and both provider implementations. Add the same enum to the bot's `ModelCfg` so that `config/model.yml` validates `reasoning_effort` as one of six enum members. The WebUI Models settings tab exposes it as a dropdown with the six values. The providers only send the parameter when the value is not `ReasoningEffort.NONE`; both an absent field and `reasoning_effort: none` map to `ReasoningEffort.NONE`, preserving current behavior for users who do not touch the setting.

## User Stories

1. As a bot operator, I want to set a `reasoning_effort` value per model in `config/model.yml`, so that I can control how much reasoning each model performs without changing code.

2. As a bot operator, I want the default state to leave current behavior unchanged, so that upgrading to this feature does not break existing models or providers.

3. As a WebUI user, I want to see a `reasoning_effort` dropdown in the Models settings tab, so that I can configure reasoning effort without hand-editing YAML.

4. As a WebUI user, I want the dropdown default to be `none`, so that the UI clearly shows that no reasoning effort is currently configured.

5. As a WebUI user, I want my selected `reasoning_effort` value to persist to `config/model.yml`, so that the setting survives restart and applies to all future turns using that model.

6. As a developer using the ModexAgent framework directly, I want `LLMConfig` to expose `reasoning_effort` as a typed `ReasoningEffort` enum, so that I can configure it when building an `LLMProvider` without the bot layer and get validation of allowed values.

7. As a framework developer, I want `create_llm_provider` to pass `reasoning_effort` as a `ReasoningEffort` enum into both `OpenAIProvider` and `LiteLLMProvider`, so that the value reaches the model API in a type-safe way.

8. As a bot maintainer, I want `BotModelConfig` to validate `reasoning_effort` as a closed enum, so that typos and invalid values are rejected at config load time.

9. As a bot maintainer, I want `reasoning_effort: none` and an absent `reasoning_effort` to behave identically, so that the UI's default dropdown value does not accidentally send an unsupported parameter to non-reasoning models.

10. As a bot maintainer, I want the reasoning chain to continue being streamed to the WebUI/IM but not persisted to memory, so that the memory layer stays unchanged and no extra storage cost is introduced.

11. As a tester, I want a unit test that exercises `BotModelConfig.synthesize_llm_config` with and without `reasoning_effort`, so that the mapping from config to `LLMConfig` is protected.

12. As a tester, I want a unit test that exercises `create_llm_provider` with `reasoning_effort`, so that the value reaches the provider constructor.

## Implementation Decisions

### 1. Typed `ReasoningEffort` enum

The framework introduces a single `StrEnum` source of truth:

```python
class ReasoningEffort(StrEnum):
    NONE = "none"
    MINIMAL = "minimal"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    XHIGH = "xhigh"
```

It lives in the framework core constants/enums module so that `LLMConfig`, `LLMProviderConfig`, provider implementations, and the bot model config all share one type. No raw strings are used for reasoning effort values anywhere.

### 2. Config schema: per-model enum value in `config/model.yml`

Each model entry may contain:

```yaml
reasoning_effort: medium
```

Allowed values are exactly the members of `ReasoningEffort`. The field has a default of `ReasoningEffort.NONE`. It lives next to `temperature`, `max_output_tokens`, and `capabilities` because it is a model-level API parameter, not a pool or agent-level behavior setting.

### 3. Backend maps `ReasoningEffort.NONE` to "do not send"

The provider is the only place that decides whether to send the parameter:

```python
if reasoning_effort != ReasoningEffort.NONE:
    params["reasoning_effort"] = reasoning_effort.value
```

`ReasoningEffort.NONE` and an absent YAML field both result in no parameter being sent. This keeps the mapping in one location and avoids scattering `none` special-casing across config models.

### 4. Framework provider wiring is pass-through

- `LLMConfig` carries `reasoning_effort: ReasoningEffort = ReasoningEffort.NONE`.
- `LLMProviderConfig` carries `reasoning_effort: ReasoningEffort = ReasoningEffort.NONE`.
- `AgentDescriptor.llm_config` carries `reasoning_effort: ReasoningEffort = ReasoningEffort.NONE`.
- `create_llm_provider` passes `reasoning_effort` to both `OpenAIProvider` and `LiteLLMProvider`.
- Both providers accept `reasoning_effort: ReasoningEffort` and apply the `NONE` guard before the API call.

### 5. Bot config reuses the framework enum

`ModelCfg` uses `ReasoningEffort` directly from the framework. This gives Pydantic validation at YAML load time, makes invalid values fail fast, and ensures the bot and framework use the same vocabulary. `BotModelConfig.synthesize_llm_config` passes the enum value into `LLMConfig` without string conversion.

### 6. WebUI Models tab exposes a dropdown

The UI adds one dropdown per model with the six enum values. The default selection is `none`. When the user changes the value and saves, the config-domain store writes it to `config/model.yml`. The frontend does not filter values per model or show a separate "not configured" option; `none` serves that role.

### 7. No reasoning-content persistence change

The reasoning chain is already surfaced via `reasoning_content`/`on_reasoning_delta` and rendered by `ReasoningBlock`. `ChatMessage.to_dict()` already strips `reasoning_content` before storage, so reasoning is not persisted to memory. This spec does not change that behavior.

### 8. No IM-side or per-turn override

Reasoning effort is configured per model only. There is no slash command, no per-turn composer control, and no `show_reasoning` toggle. This keeps the scope narrow and avoids introducing UI state that must be threaded through the pipeline.

### 9. No model capability validation

The backend does not maintain a matrix of which models support which values. If a user selects an unsupported value, the provider API returns an error. This is intentional: the complexity of staying current with provider model cards is pushed to the user, and the config layer stays simple.

## Testing Decisions

### Testing philosophy

Tests should verify external behavior: the right `ReasoningEffort` value flows from YAML through the config models into the provider, and the provider only sends the parameter when the value is not `ReasoningEffort.NONE`. The reasoning effort itself is a provider-side concern, so we do not test whether the model actually reasons more or less.

### Test seam 1: Bot config parsing and synthesis

**Existing seam**: `examples/bot_project/tests/unit/service/test_model_config.py`

Verify that:
- `config/model.yml` with `reasoning_effort: medium` parses to `ReasoningEffort.MEDIUM`.
- `reasoning_effort: invalid` raises a validation error.
- `synthesize_llm_config` produces `reasoning_effort=ReasoningEffort.MEDIUM` for `medium`, and `ReasoningEffort.NONE` for both absent and `none`.

If the test file does not yet cover `ModelCfg`, extend it rather than creating a new seam.

### Test seam 2: Framework LLM config

**Existing seam**: `tests/unit/ioc/test_llm_config.py`

Verify that `LLMConfig` accepts and exposes `reasoning_effort` as a `ReasoningEffort` enum. Add a test for `create_llm_provider` in the same seam if a factory test does not already exist; the test should assert that the constructed provider receives the configured `ReasoningEffort` value for both the `openai/` and default routing branches.

### Test seam 3: Provider pass-through

**Existing seams**: `tests/unit/providers/test_openai_provider.py` and `tests/unit/providers/test_litellm_provider_reasoning.py`

Verify that when `reasoning_effort` is a non-`NONE` enum value, the provider includes it in the API parameters, and when it is `ReasoningEffort.NONE`, the provider omits it. These seams already exercise reasoning-related provider behavior; extend them rather than adding isolated tests.

### What NOT to test

- Do not test the model's actual reasoning behavior — that is a provider concern.
- Do not test the WebUI dropdown rendering in isolation; the config-domain store and API tests cover the persistence contract.
- Do not test model-specific capability validation because that feature is explicitly out of scope.

### Prior art

- `tests/unit/ioc/test_llm_config.py` for `LLMConfig` shape tests.
- `examples/bot_project/tests/unit/service/test_model_config.py` for `BotModelConfig` parsing and synthesis tests.
- `tests/unit/providers/test_openai_provider.py` and `tests/unit/providers/test_litellm_provider_reasoning.py` for provider parameter pass-through tests.

## Out of Scope

- **Model-specific capability validation or clamping** — users choose values compatible with their model.
- **`show_reasoning` toggle** — reasoning content is always shown when present.
- **Per-turn reasoning effort override** — only per-model configuration is supported.
- **IM adapter controls** for reasoning effort.
- **Provider-specific reasoning extensions** beyond `reasoning_effort`, such as `reasoning.mode` or `reasoning.summary`.
- **Think tag scrubber** — this was discussed as a future polish item and is intentionally excluded.

## Further Notes

### Implementation order (suggested)

1. **Framework `ReasoningEffort` enum**: add the enum to core constants.
2. **Framework `LLMConfig` / `LLMProviderConfig`**: use `ReasoningEffort`.
3. **Framework `AgentDescriptor.llm_config`**: use `ReasoningEffort`.
4. **Framework `create_llm_provider`**: pass the enum to both providers.
5. **Framework providers**: accept `ReasoningEffort` and guard on `ReasoningEffort.NONE` before the API call.
6. **Bot `ModelCfg.reasoning_effort`**: use the framework `ReasoningEffort` enum.
7. **Bot `synthesize_llm_config`**: pass the enum value directly into `LLMConfig`.
8. **Bot `config/model.example.yml`**: add an example entry.
9. **WebUI Models tab**: add the dropdown with the six enum values.
10. **Tests**: cover config parsing, synthesis, and provider pass-through.
11. **Full test suite**: run `ruff`, `mypy`, and unit tests; no regressions.

### Backward compatibility

The only behavior change is that a user who explicitly sets `reasoning_effort` to a non-`NONE` value will now see that value sent to the model. Default, absent, and `ReasoningEffort.NONE` configurations are all equivalent to today's behavior.

### ADR-0021 alignment

This PRD implements the decisions recorded in ADR-0021. The ADR's `ReasoningEffort.NONE`-means-absent rule, the enum-over-string requirement, and the no-validation trade-off are reflected in the user stories and implementation decisions above.
