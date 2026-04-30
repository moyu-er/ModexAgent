<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-04-30 -->

# extensions

## Purpose
Optional integrations — LLM providers, memory backends, session storage. Not required for minimal framework operation.

## Subdirectories
| Directory | Purpose |
|-----------|---------|
| `llm/` | `LiteLLMProvider` — unified LLM API via litellm |
| `memory/` | ChromaDB, FAISS vector stores |
| `session/` | SQLAlchemy-based session persistence |

## For AI Agents

### Working In This Directory
- All sub-packages are optional — gated behind `pip install framework[extras]`
- `LiteLLMProvider`: wraps `litellm` for multi-model support (OpenAI, Anthropic, Google, etc.)
- Memory stores: ChromaDB and FAISS for vector similarity search
- Session store: SQLAlchemy[asyncio] for persistent session storage

### External
- `litellm` — LLM provider
- `chromadb`, `faiss-cpu`, `sentence-transformers` — Vector memory
- `sqlalchemy[asyncio]` — Session persistence
