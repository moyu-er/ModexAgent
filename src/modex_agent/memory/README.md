# Flexible Tiered Memory System

This package implements the **flexible-tiered-memory** architecture for the agent framework. It provides configurable memory scopes, pluggable storage backends, and a multi-layer memory pipeline.

## Architecture Overview

```
Short-term Memory ──► History Archive ──► Long-term Memory
   (ephemeral)       (token-bounded)      (append-only)       (structured KV)
```

### Layers

| Layer | Scope | Storage | Responsibility |
|-------|-------|---------|----------------|
| Short-term | `SessionScope` (default) | `FileStorage` | Bounded message history for LLM context |
| History | `UserScope` (default) | `FileStorage` | Compressed summaries with cursor tracking |
| Long-term | `UserScope` (default) | `FileStorage` | SOUL.md, USER.md, MEMORY.md |

### Key Abstractions

- **`MemoryContext`** — groups `session_id`, `user_id`, `tenant_id`, `agent_id`
- **`MemoryScope`** — extracts a scope key from a context (Session, User, Tenant, Agent, Global, Composite)
- **`MemoryStorage`** — unified async interface for messages, logs, KV, and cursors
- **`CompressionStrategy`** — pluggable short-term memory compression
- **`KnowledgeConsolidatorBase`** / **`DreamEngine`** — offline long-term memory consolidation via ReAct-based agent

## Usage

### Single-User Desktop (Default)

```python
from modex_agent.memory.system import MemorySystem

memory = MemorySystem(workspace=Path("./memory"))
await memory.initialize()
```

### Multi-Tenant SaaS

```python
from modex_agent.memory.system import MemorySystem

memory = MemorySystem(
    workspace=Path("./memory"),
    layers=MemorySystem.default_multi_tenant_layers(Path("./memory")),
)
```

## Extension Points

### Implementing a Custom `CompressionStrategy`

```python
from modex_agent.memory.core.compression import (
    CompressionStrategy, CompressionContext, CompressionResult,
)


class KeepLatestStrategy(CompressionStrategy):
    async def compress(self, messages, context):
        keep = messages[-3:] if len(messages) > 3 else messages
        pruned = messages[:-3]
        return CompressionResult(
            summary=f"Pruned {len(pruned)} messages",
            pruned_messages=pruned,
        )
```

Register it in a `MemoryLayerConfigSet`:

```python
from modex_agent.memory.layers import MemoryLayerConfigSet, SessionMemoryConfig
from modex_agent.core.scope import SessionScope

config = MemoryLayerConfigSet(
    session=SessionMemoryConfig(scope=SessionScope()),
)
```

### Implementing `RetrievalStrategy` for Vector-Backed Long-Term Memory

For applications that need semantic search over long-term memories, implement a `RetrievalStrategy` on top of a vector-capable `MemoryStorage` backend.

#### 1. Implement a Vector Storage Backend

Subclass `MemoryStorage` (or wrap an existing store) to support embeddings. The example below uses **ChromaDB** as the backing vector store:

```python
from modex_agent.memory.core.storage import MemoryStorage


class ChromaStorage(MemoryStorage):
    def __init__(self, collection_name: str, embedding_fn):
        self.collection = chromadb.Client().get_or_create_collection(collection_name)
        self.embedding_fn = embedding_fn

    async def save_messages(self, scope_key: str, messages: list[dict]):
        # Persist raw messages (optional) and index embeddings
        docs = [m["content"] for m in messages if m.get("content")]
        ids = [f"{scope_key}:{i}" for i in range(len(docs))]
        embeddings = [self.embedding_fn(d) for d in docs]
        self.collection.add(ids=ids, documents=docs, embeddings=embeddings)
```

#### 2. Define a `RetrievalStrategy`

`RetrievalStrategy` is not part of the core package yet, but is reserved as a natural extension point. A minimal contract looks like:

```python
from abc import ABC, abstractmethod
from dataclasses import dataclass

@dataclass
class RetrievalResult:
    content: str
    metadata: dict
    score: float

class RetrievalStrategy(ABC):
    @abstractmethod
    async def retrieve(
        self,
        query: str,
        context: MemoryContext,
        top_k: int = 5,
    ) -> list[RetrievalResult]:
        pass
```

#### 3. Implement `SemanticRetrieval`

```python
class SemanticRetrieval(RetrievalStrategy):
    def __init__(self, storage: ChromaStorage):
        self.storage = storage

    async def retrieve(self, query, context, top_k=5):
        results = self.storage.collection.query(
            query_embeddings=[self.storage.embedding_fn(query)],
            n_results=top_k,
        )
        return [
            RetrievalResult(
                content=doc,
                metadata={"distance": dist},
                score=1.0 - dist,
            )
            for doc, dist in zip(results["documents"][0], results["distances"][0])
        ]
```

#### 4. Wire It Into `MemorySystem`

Because `MemorySystem` currently assembles long-term memories via `KnowledgeMemoryManager.get_all()`, a `RetrievalStrategy` is best invoked **before** prompt construction:

```python
class RAGMemorySystem(MemorySystem):
    def __init__(self, retrieval: RetrievalStrategy, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.retrieval = retrieval

    async def build_system_prompt(self, context):
        base = await super().build_system_prompt(context)
        retrieved = await self.retrieval.retrieve(
            query=context.session_id,  # or a dedicated query
            context=context,
            top_k=3,
        )
        rag_section = "\n".join(r.content for r in retrieved)
        if rag_section:
            base += f"\n\n---\n\n## Relevant Memories\n{rag_section}"
        return base
```

This pattern cleanly separates the **storage backend** (ChromaDB), the **retrieval algorithm** (semantic similarity), and the **prompt assembly** (`MemorySystem`).

## Migration Notes

- `MemorySystemContextManager` is a **transitional adapter** that wraps `MemorySystem` to satisfy the legacy `ContextManager` ABC.
- Existing `AgentPipeline` code continues to work unchanged.
- New code should prefer consuming `MemorySystem` directly.
## Current Runtime Status

This memory package documents conversation and long-term memory. ReAct
suspend/resume persistence is runtime state, not user memory; use the
`TurnStateStore` from `modex_agent.runtime`.
