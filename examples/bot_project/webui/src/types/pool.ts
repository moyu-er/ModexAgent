// Pool / MCP / skills / prompt wire types. These are the JSON shapes that
// cross the REST API, used by the API clients in
// `lib/{poolApi,mcpApi,skillsApi,promptsApi}.ts`. Pool TREES are declared in
// the scope declaration (edited via `scopeApi.ts`); only the listing summary
// crosses this API since ticket 11.

// ─── External providers ──────────────────────────────────────────────────────

export type ProviderKind = "pi" | "opencode";

// ─── Skill source / origin (closed literal sets) ─────────────────────────────

export type SkillSource = "global" | "local";
export type SkillOrigin = "repo" | "user";

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

export interface PoolSummary {
  name: string;
  root_agent_name: string;
  subagent_count: number;
}

// ─── Prompt ──────────────────────────────────────────────────────────────────

export interface PromptContent {
  name: string;
  content: string;
}

export interface PromptSummary {
  name: string;
  size_bytes: number;
  mtime: string;
}

export interface PromptUsage {
  pool: string;
  agent_kind: "main" | "subagent";
  agent_name: string;
}

// ─── Skills ──────────────────────────────────────────────────────────────────

export interface SkillEntry {
  name: string;
  source: SkillSource;
  origin?: SkillOrigin;
  description?: string;
}
