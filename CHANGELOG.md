# Changelog

## Breaking — Core Memory rename (ADR-0035)

The `memory.knowledge:` YAML section is renamed to `memory.core:`. This is
a one-time breaking change to disambiguate the in-context agent identity
layer from the forthcoming KnowledgeBase (RAG) module.

**Why:** The framework historically used "knowledge" for two different
concepts. Per ADR-0035, the in-context agent identity / user-profile layer
(`SOUL.md` / `USER.md` / `MEMORY.md`) is renamed to **Core Memory** to align
with Letta's terminology and free "knowledge base" for its industry-standard
RAG meaning (forthcoming ADR-0036).

### Migration

#### YAML config

```yaml
# Before
memory:
  knowledge:
    enabled: true

# After
memory:
  core:
    enabled: true
```

#### Python imports

```python
# Before
from modex_agent.ioc.configs.memory import KnowledgeConfig
from modex_agent.memory.layers.knowledge import ScopedKnowledgeMemoryManager

# After
from modex_agent.ioc.configs.memory import CoreMemoryConfig
from modex_agent.memory.layers.core import ScopedCoreMemoryManager
```

#### Archive files on disk (only if you used archive with the KNOWLEDGE channel)

If your workspace has `memory/<pool>/archive/knowledge_archive.jsonl`, rename
it to `core_archive.jsonl`:

```bash
mv <workspace>/memory/<pool>/archive/knowledge_archive.jsonl <workspace>/memory/<pool>/archive/core_archive.jsonl
```

The `channel` field value inside `*_archive.jsonl` files also changed from
`"knowledge"` to `"core"`. Old files with `channel: "knowledge"` will be
unreadable after upgrade — either rename via the bash command above (for the
default file) or write a one-shot migration script to update the JSONL
content if you have many files.

#### What is NOT renamed

- XML tag values in prompts: `<your_identity>`, `<user_profile>`, `<known_facts>` — agent-facing, unchanged.
- File names `SOUL.md` / `USER.md` / `MEMORY.md` — user workspace data, unchanged.
- `long_term → archive + knowledge` migration warning — historical, fires only for very old configs, unchanged.

See `docs/adr/0035-core-memory-and-knowledge-base-terminology-split.md` for
the full rationale and rename map.
