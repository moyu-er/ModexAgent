// Per-category visual identity for the settings nav + page heads.
//
// Each settings domain (IM, Models, Pools, MCP, Skills) maps to an icon
// component (lucide), an inline `--cat` CSS value (drives .category-chip /
// .page-head-icon tinting via the --color-cat-* tokens), and a pair of i18n
// keys (titleKey / subKey) resolved at render time via useT(). The text is
// not stored here — it lives in the i18n catalog (src/i18n/en.ts).
//
// `ViewKey` is the single source of truth for the settings domains; SettingsView
// re-exports it so existing imports keep working.

import type { ComponentType } from "react";
import {
  MessagesSquare,
  Cpu,
  Boxes,
  Command,
  Sparkles,
  type LucideProps,
} from "lucide-react";
import type { MessageKey } from "../../i18n";
import { TERMS } from "../../i18n/terms";

export type ViewKey = "im" | "model" | "pools" | "mcp" | "skills";

export interface CategoryMeta {
  icon: ComponentType<LucideProps>;
  catVar: string;
  titleKey?: MessageKey;
  titleTerm?: string;
  subKey: MessageKey;
}

export const CATEGORY: Record<ViewKey, CategoryMeta> = {
  im: {
    icon: MessagesSquare,
    catVar: "var(--color-cat-im)",
    titleKey: "settings.im.title",
    subKey: "settings.im.sub",
  },
  model: {
    icon: Cpu,
    catVar: "var(--color-cat-models)",
    titleKey: "settings.models.title",
    subKey: "settings.models.sub",
  },
  pools: {
    icon: Boxes,
    catVar: "var(--color-cat-pools)",
    titleKey: "settings.pools.title",
    subKey: "settings.pools.sub",
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
};
