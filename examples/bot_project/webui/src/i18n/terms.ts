// Universal proper nouns — single source of truth, never translated.
//
// What belongs here: cross-section product/protocol names that are the same
// in every locale (e.g. "MCP", "Skill", "Skills"). These nouns appear as
// standalone labels (nav items, page titles) and inside catalog sentences.
// In every locale the term itself stays as-is; only the surrounding words
// translate.
//
// What stays in per-locale catalogs (en.ts etc.): sentences that embed these
// terms (e.g. "No MCP servers configured." — the words around "MCP" translate,
// "MCP" does not), and section-local protocol/product labels (e.g. "stdio",
// "SSE", "OpenAI Compatible") that are labels within one domain rather than
// cross-section proper nouns.

export const TERMS = {
  mcp: "MCP",
  skill: "Skill",
  skills: "Skills",
} as const;

export type TermKey = keyof typeof TERMS;
