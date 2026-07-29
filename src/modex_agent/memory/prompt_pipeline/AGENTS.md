<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-06-22 | Updated: 2026-07-12 -->

# prompt_pipeline

## Purpose
System prompt pipeline — an ordered, versioned collection of `SystemPromptProvider` instances. Each provider contributes one section of the system prompt (base personality, runtime metadata, workspace info, todo discipline, remote-agent reply contract, etc.). Supports caching, version-based refresh, and ordered assembly into the final system prompt string.

## Key Files
| File | Description |
|------|-------------|
| `__init__.py` | Package init |
| `providers.py` | Concrete `SystemPromptProvider` implementations (see below) |

## Providers

| Provider | Version key | Fires when | Content |
|----------|-------------|------------|---------|
| `BasePromptProvider` | `static` | Always | Static base prompt from `agents/<name>.md` (never refreshes) |
| `RuntimeProvider` | hourly | Always | Current date/hour + platform info |
| `ModelInfoProvider` | `model:{name}:{modalities}` / `model:none` | `runtime_info` present (main-agent turn) | Declares the agent's perceptual capabilities (e.g. image perception). `ModelInfo` supplied at construction from `runtime_info[RuntimeInfoKey.MODEL_INFO]` (threaded by `assemble_context` from `runtime_services.model_info`). Tools that behave differently per modality (e.g. `read`) are mentioned as examples, not bound. Emits nothing when `model_info` is `None`. |
| `SkillProvider` | skill-set hash | Skills assigned | Active skill summaries |
| `ExperienceProvider` | experience hash | Experience files exist | Relevant experience entries |
| `CoreMemoryProvider` | core memory hash | Core Memory files exist | SOUL.md / USER.md / MEMORY.md content (provider renamed from `KnowledgeProvider` per ADR-0035) |
| `ArchiveProvider` | retrieved content hash | Archive entries exist | Backend-neutral historical summaries; `context.md` paths only when file storage exposes one |
| `PrunedProvider` | pruned hash | Pruned catalog exists | XML catalog of cleaned-up messages (priority 85) |
| `RuntimeProvider` | hourly | Always | Runtime metadata |
| `ProviderBlocksProvider` | blocks hash | Provider blocks configured | Custom prompt blocks |
| `ProviderPrefetchProvider` | prefetch hash | Prefetch configured | Prefetched context |
| `TodoAwareSystemPromptProvider` | `todo-enabled` / `no-todo` | Agent owns `todo_read` + `todo_write` tools | Task-discipline reminder |
| `AgentCommunicationSystemPromptProvider` | `comm:<fragments>` / `comm:none` | `send_to_agent` tool has targets matching sub-module conditions | Composite provider with 3 internal sub-modules: peer reply contract (targets with `bus_ref`), subagent dispatch contract (NON-subagent + SUBAGENT targets), subagent consultation contract (SUBAGENT kind). Version is `"comm:"` + `|`-joined sub-module fragments; content joins applying sections. Replaces the deleted `PeerCommunicationSystemPromptProvider`. |

## For AI Agents

### Working In This Directory
- `SystemPromptPipeline` holds an ordered list of `SystemPromptProvider` instances
- Each provider has a `version` — the pipeline only re-fetches content when the version changes
- Providers are ordered by their position in the pipeline list (not by priority)
- `BasePromptProvider` is static (version="static") — never refreshes
- `RuntimeProvider` refreshes hourly based on the current datetime hour
- Gated providers (Todo, AgentCommunication) emit empty content when their
  condition is unmet — no-op for agents that don't own the relevant tools

### Common Patterns
- Create providers, add them to a `SystemPromptPipeline` in the desired order, call `pipeline.get_content()` to get the assembled system prompt
- `_fetch_version()` returns a version string; `_fetch_content()` returns the actual prompt text
- Pipeline caches content per version — call `invalidate()` to force refresh
- New providers should follow the `SystemPromptProvider` ABC pattern: gated
  by a condition, version derived from the gating data, content empty when
  condition unmet

## Dependencies

### Internal
- `modex_agent.core.prompt` — `SystemPromptProvider`, `SystemPromptPipeline`
- `modex_agent.utils.timezone` — `get_user_timezone`
- `modex_agent.core.tool_manager` — `ToolManager` (for Todo + PeerComm providers)
- `modex_agent.multi_agent.tools` — `SendToAgentTool`, `CommunicationTarget` (for PeerComm provider)

<!-- MANUAL -->
