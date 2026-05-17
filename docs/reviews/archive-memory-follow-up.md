# Archive Memory Review Follow-up

## Review Summary

- The context/knowledge split is directionally correct, but the implementation must enforce an archive pair invariant before it can replace the old archive path.
- The old single-summary archive path still exists in coordinator fallback behavior and must be removed from default framework behavior.
- Archive input selection must be stricter: unknown roles are dropped, tool results are summarized into bounded structured evidence, and raw tool output is not copied wholesale into generation prompts.
- Archive storage needs a typed channel-storage contract instead of dynamic `getattr` calls and loose payloads.
- Bot project documentation still contains old `ArchiveStrategy`, `history.jsonl`, and `SummarizerStrategy` wording and must be cleaned.

## Fix Plan

1. Add regression tests for partial/empty archive generation, direct coordinator construction without archive generation, unknown role filtering, and tool-result summarization.
2. Make default archive generation all-or-nothing: either both context and knowledge writes are present, or no archive is produced and session compression does not commit.
3. Remove the coordinator's single-summary archive fallback for configured archive layers.
4. Add a typed `ArchiveChannelStorage` protocol and route archive state/channel logs through it.
5. Clean stale docs and verify `tests/unit/memory`, `mypy framework/memory`, and focused ruff checks.
