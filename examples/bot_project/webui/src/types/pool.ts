// Pool / MCP / skills / prompt wire types — mirror the backend payload shapes
// in `bot/config/pool_payloads.py`. These are the JSON shapes that cross the
// REST API (Phase 2B), used by the API clients in `lib/{poolApi,mcpApi,skillsApi}.ts`.

// ─── Tool presets / context modes / skill source (closed literal sets) ───────

export type ToolPreset = "full" | "read_write" | "read_only" | "minimal" | "none";
export type ContextMode = "fresh" | "fork";
export type SystemPromptMode = "replace" | "append";
export type SkillSource = "global" | "local";

// ─── Approval ────────────────────────────────────────────────────────────────

export interface ApprovalEntry {
  allowed_paths: string[];
}

export interface ApprovalConfig {
  enabled: boolean;
  tools: Record<string, ApprovalEntry>;
}

// ─── MCP registry entry ──────────────────────────────────────────────────────
//
// The backend stores the transport discriminator on disk under the `type` key
// (matching registry.json) and exposes it on the model as `transport`. Both
// names are accepted on input via a pydantic alias.
//
// TS modeling decision: there is no clean per-field aliasing in TS, so the
// input/entry type accepts BOTH `transport` and `type` as optional fields
// (whichever the caller set is forwarded as-is), and `McpServerEntry` is the
// canonical shape used internally. The API client always sends `type` (the
// on-disk wire key) so reads/writes round-trip through the same field.
// `env` is the in-memory name; on disk it is `environment`.

export type McpTransport = "stdio" | "sse" | "streamableHttp";

/**
 * Canonical MCP server entry — the shape we use internally and send over the
 * wire. We send `type` (the registry.json key) so the backend alias maps it
 * onto `transport` cleanly.
 */
export interface McpServerEntry {
  /** Transport discriminator; sent as `type` on the wire. */
  transport?: McpTransport;
  command?: string;
  args?: string[];
  /** In-memory name; serialized as `environment` on disk. */
  env?: Record<string, string>;
  cwd?: string;
  url?: string;
  headers?: Record<string, string>;
  timeout?: number;
}

// ─── Pool tree ───────────────────────────────────────────────────────────────

export interface MainAgentNode {
  agent_name: string;
  max_steps: number;
  use_terminal: boolean;
  terminal_visibility: boolean;
  tool_preset: ToolPreset;
  tool_supplements: string[];
  approval?: ApprovalConfig | null;
  mcp: string[];
}

export interface SubagentNode {
  agent_name: string;
  description: string;
  max_steps: number;
  tool_preset: ToolPreset;
  tool_supplements: string[];
  context_mode: ContextMode;
  mcp: string[];
  /** System-prompt assembly vs the parent's. Omitted on the wire when "replace" (default). */
  system_prompt_mode?: SystemPromptMode;
  /** Parent-context truncation cap; 1..100, default 80. Only meaningful when context_mode === "fork". Omitted on the wire when at default. */
  fork_max_messages?: number;
}

export interface PoolTree {
  name: string;
  main_agent_name: string;
  main: MainAgentNode;
  subagents: SubagentNode[];
  restart_required: boolean;
}

export interface PoolSummary {
  name: string;
  main_agent_name: string;
  subagent_count: number;
}

// ─── Prompt ──────────────────────────────────────────────────────────────────

export interface PromptContent {
  name: string;
  content: string;
}

// ─── Skills ──────────────────────────────────────────────────────────────────

export interface SkillEntry {
  name: string;
  source: SkillSource;
}
