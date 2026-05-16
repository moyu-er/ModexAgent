<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-05-16 -->

# providers

## Purpose
LLM provider implementations — abstracts model invocation behind a common interface. Supports LiteLLM (100+ models) and native OpenAI SDK, with shared streaming delta types and error handling.

## Key Files
| File | Description |
|------|-------------|
| `litellm_provider.py` | `LiteLLMProvider` — unified provider for 100+ models via LiteLLM |
| `openai_provider.py` | `OpenAIProvider` — native OpenAI SDK provider |

## Subdirectories
| Directory | Purpose |
|-----------|---------|
| `shared/` | Shared provider utilities |
| `shared/delta.py` | Streaming delta types for chunk-based responses |
| `shared/errors.py` | Shared error handling and retry logic |

## For AI Agents
- Both providers expose a common LLM call interface; consumers should not depend on provider-specific types
- `shared/delta.py` normalizes streaming chunks across providers
- `shared/errors.py` provides unified error classification and retry strategies
