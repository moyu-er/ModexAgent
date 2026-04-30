<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-04-30 -->

# utils

## Purpose
General-purpose utilities — media processing, tokenization, helpers. No domain-specific logic.

## Key Files
| File | Description |
|------|-------------|
| `media_utils.py` | `MediaProcessor` — handles image/audio/video attachments, multi-modal content building |
| Other files | Token counting, string helpers, etc. |

## For AI Agents

### Working In This Directory
- `MediaProcessor`: processes file attachments, extracts document text, builds OpenAI-compatible multi-modal content
- Keep utilities stateless and free of framework dependencies
