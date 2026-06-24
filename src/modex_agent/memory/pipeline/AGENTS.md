<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-06-22 | Updated: 2026-06-22 -->

# pipeline

## Purpose
System prompt pipeline — an ordered, versioned collection of `SystemPromptProvider` instances. Each provider contributes one section of the system prompt (base personality, runtime metadata, workspace info, etc.). Supports caching, version-based refresh, and ordered assembly into the final system prompt string.

## Key Files
| File | Description |
|------|-------------|
| `__init__.py` | Package init |
| `abc.py` | Re-exports `SystemPromptProvider` from `framework.core.prompt` |
| `pipeline.py` | Re-exports `SystemPromptPipeline` from `framework.core.prompt` |
| `providers.py` | Concrete `SystemPromptProvider` implementations — `BasePromptProvider` (static base prompt, never refreshes), `RuntimeProvider` (current date/hour + platform, refreshes hourly), plus additional providers for workspace and tool metadata |

## For AI Agents

### Working In This Directory
- `SystemPromptPipeline` holds an ordered list of `SystemPromptProvider` instances
- Each provider has a `version` — the pipeline only re-fetches content when the version changes
- Providers are ordered by their position in the pipeline list (not by priority)
- `BasePromptProvider` is static (version="static") — never refreshes
- `RuntimeProvider` refreshes hourly based on the current datetime hour

### Common Patterns
- Create providers, add them to a `SystemPromptPipeline` in the desired order, call `pipeline.get_content()` to get the assembled system prompt
- `_fetch_version()` returns a version string; `_fetch_content()` returns the actual prompt text
- Pipeline caches content per version — call `invalidate()` to force refresh

## Dependencies

### Internal
- `framework.core.prompt` — `SystemPromptProvider`, `SystemPromptPipeline`
- `framework.utils.timezone` — `get_user_timezone`

<!-- MANUAL -->
