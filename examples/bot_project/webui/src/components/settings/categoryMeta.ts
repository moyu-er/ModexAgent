// Per-category visual identity for the settings nav + page heads.
//
// Each settings domain (IM, Models, Pools, MCP, Skills) maps to an icon
// component (lucide), an inline `--cat` CSS value (drives .category-chip /
// .page-head-icon tinting via the --color-cat-* tokens), and a short
// title/subtitle pair used by both the sidebar nav and each view's page head.
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

export type ViewKey = "im" | "model" | "pools" | "mcp" | "skills";

export interface CategoryMeta {
  icon: ComponentType<LucideProps>;
  /** CSS value for the inline --cat variable, e.g. "var(--color-cat-mcp)". */
  catVar: string;
  title: string;
  sub: string;
}

export const CATEGORY: Record<ViewKey, CategoryMeta> = {
  im: {
    icon: MessagesSquare,
    catVar: "var(--color-cat-im)",
    title: "IM Adapters",
    sub: "Messaging platform connections",
  },
  model: {
    icon: Cpu,
    catVar: "var(--color-cat-models)",
    title: "Models",
    sub: "Providers, models & default routing",
  },
  pools: {
    icon: Boxes,
    catVar: "var(--color-cat-pools)",
    title: "Pools",
    sub: "Agent pools & routing",
  },
  mcp: {
    icon: Command,
    catVar: "var(--color-cat-mcp)",
    title: "MCP",
    sub: "Model Context Protocol servers",
  },
  skills: {
    icon: Sparkles,
    catVar: "var(--color-cat-skills)",
    title: "Skills",
    sub: "Agent skills & capabilities",
  },
};
