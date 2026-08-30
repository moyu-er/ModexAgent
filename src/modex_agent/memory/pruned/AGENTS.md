<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-06-22 | Updated: 2026-06-22 -->

# pruned

## Purpose
Manages the pruned memory catalog — records of cleaned-up session messages that were removed during compaction. The catalog is session-scoped (per-session sub-directories), independent of archive (works even when archiving is off), and provides XML injection data so agents can reference pruned context.

## Key Files
| File | Description |
|------|-------------|
| `__init__.py` | Package init |
| `manager.py` | `PrunedManager` — orchestrates writing pruned batches, building the index, eviction, and generating injection XML. Shared by cleanup and injection subsystems. Per-session `FilePrunedStorage` created lazily |
| `models.py` | `PrunedIndexEntry` — frozen dataclass for a single pruned batch. Fields: id, cleanup_time, message_count, content_filename, optional time range and topic. Serialized as JSONL in `index.jsonl` |
| `render.py` | `render_transcript()` — pure-function renderer: message dicts → markdown transcript (three-line header + numbered `## [NNN]` message blocks). No IO, no COMPACT filtering — `PrunedManager.write_pruned` owns that invariant |
| `storage.py` | `PrunedStorage` ABC + `FilePrunedStorage` — abstract interface and concrete file-based implementation for persisting markdown transcripts (`.md`, via `write_transcript`) and their JSONL index |

## For AI Agents

### Working In This Directory
- Pruned catalog is independent of the archive system — works with archive off or failed
- Topic falls back to time range when no CONTEXT archive is available
- Injection priority: 85 (between core memory=100 and archive=70)
- The XML catalog points agents to per-session `pruned/{session_id}/` directories for reading full pruned content
- Storage is session-scoped: one `PrunedManager` instance serves all sessions under a `pruned_base_dir`
- COMPACT exclusion invariant: `write_pruned` drops COMPACT-role messages at its entry (pruned content = original conversation memory only); an all-COMPACT batch writes no file and no index entry
- `grep "^## \[" <file>` on a transcript yields a line-numbered message table-of-contents — transcripts are information-dense, read narrow windows instead of whole files

### Common Patterns
- `PrunedManager.write_pruned(messages)` — called from `cleanup_session()` after messages are pruned
- `PrunedManager.get_injection_xml(session_id)` — called during injection to generate pruned catalog XML
- Index entries are append-only; eviction removes old entries and their associated files
- `FilePrunedStorage.write_transcript` writes rendered markdown transcripts (`.md`); the index stays JSONL as `index.jsonl`

## Dependencies

### Internal
- `modex_agent.memory.tags` — `PrunedTag`
- `modex_agent.memory.pruned.models` — `PrunedIndexEntry`
- `modex_agent.memory.pruned.storage` — `PrunedStorage` (ABC) + `FilePrunedStorage`
- `modex_agent.utils.timezone` — `get_user_timezone`
- `modex_agent.utils.xml` — XML formatting helpers
- `modex_agent.utils.file_io` — `read_jsonl_robust`

<!-- MANUAL -->
