// Per-category visual identity for the settings nav + page heads.
//
// Each settings domain maps to an icon component (lucide), an inline `--cat`
// CSS value (drives .category-chip / .page-head-icon tinting via the
// --color-cat-* tokens), and a pair of i18n keys (titleKey / subKey)
// resolved at render time via useT(). The text is not stored here — it lives
// in the i18n catalog (src/i18n/en.ts).
//
// `ViewKey` is the single source of truth for the settings domains.

import type { ComponentType } from "react";
import {
  MessagesSquare,
  Cpu,
  Command,
  Sparkles,
  FileText,
  ListTree,
  Boxes,
  Settings2,
  Bot,
  Puzzle,
  SlidersHorizontal,
  type LucideProps,
} from "lucide-react";
import type { MessageKey } from "../../i18n";
import { TERMS } from "../../i18n/terms";

export type ViewKey =
  | "general"
  | "model"
  | "assistants"
  | "extensions"
  | "im"
  | "advanced"
  | "pools"
  | "mcp"
  | "skills"
  | "prompts"
  | "scope";

export interface CategoryMeta {
  icon: ComponentType<LucideProps>;
  catVar: string;
  titleKey?: MessageKey;
  titleTerm?: string;
  subKey: MessageKey;
  /** Search keywords used by the settings search box (display-language). */
  keywords?: string[];
}

export const CATEGORY: Record<ViewKey, CategoryMeta> = {
  general: {
    icon: Settings2,
    catVar: "var(--color-cat-models)",
    titleKey: "settings.general.title",
    subKey: "settings.general.sub",
    keywords: ["language", "theme", "default", "workspace", "assistant", "pool"],
  },
  model: {
    icon: Cpu,
    catVar: "var(--color-cat-models)",
    titleKey: "settings.models.title",
    subKey: "settings.models.sub",
    keywords: ["provider", "llm", "api key", "default model"],
  },
  assistants: {
    icon: Bot,
    catVar: "var(--color-cat-pools)",
    titleKey: "settings.poolsPanel.title",
    subKey: "settings.poolsPanel.sub",
    keywords: ["agent", "pool", "prompt", "collaborator", "subagent", "memory", "approval"],
  },
  extensions: {
    icon: Puzzle,
    catVar: "var(--color-cat-skills)",
    titleKey: "settings.extensions.title",
    subKey: "settings.extensions.sub",
    keywords: ["skill", "mcp", "server", "prompt"],
  },
  im: {
    icon: MessagesSquare,
    catVar: "var(--color-cat-im)",
    titleKey: "settings.im.title",
    subKey: "settings.im.sub",
    keywords: ["qq", "telegram", "channel", "messaging"],
  },
  advanced: {
    icon: SlidersHorizontal,
    catVar: "var(--color-cat-scope)",
    titleKey: "settings.advanced.title",
    subKey: "settings.advanced.sub",
    keywords: ["yaml", "declaration", "raw", "scope"],
  },
  scope: {
    icon: ListTree,
    catVar: "var(--color-cat-scope)",
    titleKey: "settings.scope.title",
    subKey: "settings.scope.sub",
    keywords: ["tree", "bill", "provenance", "declaration"],
  },
  // Legacy view keys — kept for the embedded page heads of editors now
  // hosted inside the six groups (assistants → pools; extensions →
  // mcp/skills/prompts). Never listed in the settings nav.
  pools: {
    icon: Boxes,
    catVar: "var(--color-cat-pools)",
    titleKey: "settings.poolsPanel.title",
    subKey: "settings.poolsPanel.sub",
  },
  mcp: {
    icon: Command,
    catVar: "var(--color-cat-mcp)",
    titleTerm: TERMS.mcp,
    subKey: "settings.mcp.sub",
  },
  skills: {
    icon: Sparkles,
    catVar: "var(--color-cat-skills)",
    titleTerm: TERMS.skills,
    subKey: "settings.skills.sub",
  },
  prompts: {
    icon: FileText,
    catVar: "var(--color-cat-prompts)",
    titleKey: "settings.prompts.title",
    subKey: "settings.prompts.sub",
  },
};

/** The MCP + Skills icons stay reachable for the extensions group page. */
export const EXTENSION_ICONS = {
  mcp: { icon: Command, catVar: "var(--color-cat-mcp)" },
  skills: { icon: Sparkles, catVar: "var(--color-cat-skills)" },
  prompts: { icon: FileText, catVar: "var(--color-cat-prompts)" },
} as const;

/** Legacy pools-group icon (Boxes) retained for the assistants page head. */
export const POOLS_ICON = Boxes;
