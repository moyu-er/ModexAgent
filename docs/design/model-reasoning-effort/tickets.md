# Tickets: Model-level Reasoning Effort Configuration

A per-model `reasoning_effort` setting, editable from the WebUI Models tab, persisted in `config/model.yml`, and wired through to `LLMProvider` so it reaches the model API when configured. Default, absent, and `ReasoningEffort.NONE` all preserve current behavior (no parameter sent).

Source: `docs/design/model-reasoning-effort/PRD.md` and ADR-0021 (`docs/adr/0021-model-level-reasoning-effort.md`).

Work the **frontier**: any ticket whose blockers are all done. For this linear chain that means top to bottom.

## 1. Constant-ize `reasoning_effort` provider parameter key

**What to build:** Replace the hard-coded `"reasoning_effort"` strings used as API parameter keys in the OpenAI and LiteLLM providers with a single named constant. This is a zero-behavior prefactor that removes magic strings before the enum work lands.

**Blocked by:** None — can start immediately.

- [x] A single constant represents the API parameter name and is used by both providers.
- [x] Existing provider tests still pass with no behavior change.

## 2. Introduce `ReasoningEffort` enum and wire through the framework

**What to build:** Add a typed `ReasoningEffort` StrEnum to the framework and propagate it through `LLMConfig`, `LLMProviderConfig`, `AgentDescriptor.llm_config`, `create_llm_provider`, and both provider implementations. Providers only send the parameter when the value is not `ReasoningEffort.NONE`.

**Blocked by:** 1. Constant-ize `reasoning_effort` provider parameter key.

- [x] `ReasoningEffort` enum lives in the framework core constants/enums module with values `NONE`, `MINIMAL`, `LOW`, `MEDIUM`, `HIGH`, `XHIGH`.
- [x] `LLMConfig`, `LLMProviderConfig`, and `AgentDescriptor.llm_config` carry `reasoning_effort: ReasoningEffort = ReasoningEffort.NONE`.
- [x] `create_llm_provider` passes the enum to both providers.
- [x] Providers accept `ReasoningEffort` and emit the parameter only for non-`NONE` values.
- [x] Framework unit tests cover `LLMConfig` shape and provider pass-through.

## 3. Wire `ReasoningEffort` into bot model config

**What to build:** Reuse the framework `ReasoningEffort` enum in the bot's `ModelCfg`, update `synthesize_llm_config` to pass the enum value into `LLMConfig`, and add an example entry to `config/model.example.yml`. Invalid values should fail at YAML load time.

**Blocked by:** 2. Introduce `ReasoningEffort` enum and wire through the framework.

- [x] `ModelCfg.reasoning_effort` uses the framework `ReasoningEffort` enum.
- [x] `synthesize_llm_config` maps both an absent field and `reasoning_effort: none` to `ReasoningEffort.NONE`; other values pass through as-is.
- [x] `config/model.example.yml` contains an example `reasoning_effort` entry.
- [x] Bot config unit tests cover parsing, validation, and synthesis mapping.

## 4. Add `reasoning_effort` dropdown to WebUI Models tab

**What to build:** Add a dropdown for `reasoning_effort` in the WebUI Models settings page, with the six enum values and a default selection of `none`. Changes should persist through the existing config-domain store to `config/model.yml`.

**Blocked by:** 3. Wire `ReasoningEffort` into bot model config.

- [x] The Models tab shows a `reasoning_effort` dropdown per model with options `none`, `minimal`, `low`, `medium`, `high`, `xhigh`.
- [x] The default selection is `none`.
- [x] Selecting a value and saving writes the corresponding value to `config/model.yml`.
- [x] Frontend tests pass.

## 5. End-to-end verification and regression checks

**What to build:** Run the full verification suite — `ruff`, `mypy`, and all relevant unit/integration tests — to confirm the default behavior is unchanged and that a configured `reasoning_effort` value flows end-to-end from the YAML config to the provider API call.

**Blocked by:** 4. Add `reasoning_effort` dropdown to WebUI Models tab.

- [x] `ruff check` passes with no new warnings.
- [x] `mypy` passes on touched modules (no new reasoning-effort related errors; pre-existing annotation issues remain).
- [x] All framework, bot, and provider unit tests pass.
- [x] A configured non-`NONE` value reaches the provider API; `NONE`/absent omits the parameter.
