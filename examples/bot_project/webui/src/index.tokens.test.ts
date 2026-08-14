/**
 * Token-completeness guard for the Teal & Ember redesign (DESIGN.md §2–§4).
 *
 * Reads src/index.css as text, extracts the `:root` (light) and `.dark`
 * blocks, and asserts that every required token is defined in BOTH themes.
 * Also pins the canonical brand values so the palette cannot silently drift
 * back to the legacy emerald set.
 */
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

// happy-dom patches URL, so resolve from the vitest cwd (the webui root)
// instead of import.meta.url.
const css = readFileSync(resolve(process.cwd(), "src/index.css"), "utf8");

function extractBlock(selector: string): string {
  // Match ":root { ... }" / ".dark { ... }" at top level (non-nested braces
  // inside token blocks are not used in this file).
  const re = new RegExp(`^${selector.replace(".", "\\.")}\\s*\\{([^}]*)\\}`, "m");
  const match = css.match(re);
  if (!match || match[1] === undefined) {
    throw new Error(`block ${selector} not found in index.css`);
  }
  return match[1];
}

const light = extractBlock(":root");
const dark = extractBlock(".dark");

/** Full Teal & Ember token set — DESIGN.md §2 (colors), §4 (radius/elevation/motion). */
const REQUIRED_TOKENS = [
  // Core palette (§2.1)
  "--color-canvas",
  "--color-canvas-sidebar",
  "--color-canvas-elevated",
  "--color-canvas-popover",
  "--color-ink",
  "--color-body",
  "--color-mute",
  "--color-faint",
  "--color-brand",
  "--color-brand-deep",
  "--color-brand-bright",
  "--color-ember",
  "--color-danger",
  "--color-warning",
  "--color-success",
  // Borders — the "teal shimmer" rule (§2.2)
  "--color-hairline",
  "--color-border-strong",
  // Selection (§2.3)
  "--color-selection",
  // Approval severities (§2.3)
  "--color-severity-normal",
  "--color-severity-sensitive",
  "--color-severity-dangerous",
  "--color-severity-hardline",
  // Domain category colors (§2.3)
  "--color-cat-mcp",
  "--color-cat-pools",
  "--color-cat-skills",
  "--color-cat-models",
  "--color-cat-im",
  "--color-cat-prompts",
  // Message bubbles (§6)
  "--color-user-bubble",
  "--color-user-bubble-text",
  // Radius scale (§4)
  "--radius-xs",
  "--radius-sm",
  "--radius-md",
  "--radius-lg",
  "--radius-pill",
  // Elevation (§4)
  "--shadow-card",
  "--shadow-card-hover",
  "--shadow-popover",
  // Motion (§4)
  "--dur-fast",
  "--dur",
  "--dur-slow",
  "--ease-out",
  // Type scale (§3) — 7 tiers
  "--text-xs",
  "--text-sm",
  "--text-base",
  "--text-md",
  "--text-lg",
  "--text-xl",
  "--text-2xl",
  // Line-height (§3) — 4 tiers
  "--leading-tight",
  "--leading-snug",
  "--leading-relaxed",
  "--leading-prose",
  // Letter-spacing (§3) — 4 tiers
  "--tracking-tight",
  "--tracking-normal",
  "--tracking-wide",
  "--tracking-eyebrow",
  // Graph visualization semantic tokens (graph PRD §7.1)
  "--color-graph-node-fill",
  "--color-graph-node-fill-done",
  "--color-graph-node-border",
  "--color-graph-node-border-active",
  "--color-graph-edge",
  "--color-graph-edge-active",
  "--color-graph-arrow",
  "--color-graph-arrow-active",
  "--color-graph-deliver",
  "--color-graph-deliver-glow",
  "--color-graph-deliver-trail",
  "--color-graph-active-ring",
  "--color-graph-mini-node",
  "--color-graph-mini-edge",
  "--color-graph-mini-start",
  "--color-graph-mini-end",
  // Graph motion tokens (graph PRD §7.2)
  "--dur-deliver",
  "--ease-deliver",
  "--dur-ring-pulse",
  "--ease-ring-pulse",
  "--dur-layout",
] as const;

/** Legacy emerald values that must not survive the retokening. */
const LEGACY_EMERALD = ["#059669", "#10b981", "#047857", "#34d399", "#065f46"];

/** Legacy "Midnight Ink" (cyan/slate) values replaced by Warm Graphite —
 *  catching them here prevents the old cyan brand from sneaking back. */
const LEGACY_MIDNIGHT = ["#22d3ee", "#06b6d4", "#67e8f9", "#0a0f1e", "#070b17"];

/** Legacy font families replaced by the Inter single-family system (§3).
 *  Catching them here prevents a stale import or stack from sneaking back. */
const LEGACY_FONTS = ["Geist Mono", "Space Grotesk"];

describe("Teal & Ember design tokens (index.css)", () => {
  it.each(REQUIRED_TOKENS)(":root (light) defines %s", (token) => {
    expect(light).toContain(`${token}:`);
  });

  it.each(REQUIRED_TOKENS)(".dark defines %s", (token) => {
    expect(dark).toContain(`${token}:`);
  });

  it("pins the canonical brand values from DESIGN.md §2.1", () => {
    // Dark theme: Warm Graphite — same teal brand spine as light, lifted
    // for dark-surface contrast, over a neutral warm-grey canvas
    expect(dark).toContain("--color-brand: #2DD4BF");
    expect(light).toContain("--color-brand: #0D9488");
    expect(dark).toContain("--color-ember: #F59E0B");
    expect(light).toContain("--color-ember: #B45309");
    expect(dark).toContain("--color-canvas: #1B1B1D");
  });

  it("uses neutral white-alpha hairlines in dark mode (§2.2 — Warm Graphite)", () => {
    // Dark theme keeps hue-free white-alpha borders so the teal accent reads
    // as the single colored thread, not as a tint over every surface.
    expect(dark).toContain("--color-hairline: rgba(255, 255, 255, 0.08)");
    expect(dark).toContain("--color-border-strong: rgba(255, 255, 255, 0.16)");
  });

  it("leaves no legacy emerald values behind", () => {
    for (const hex of LEGACY_EMERALD) {
      expect(css.toLowerCase()).not.toContain(hex.toLowerCase());
    }
  });

  it("leaves no legacy Midnight Ink (cyan) values behind", () => {
    for (const hex of LEGACY_MIDNIGHT) {
      expect(css.toLowerCase()).not.toContain(hex.toLowerCase());
    }
  });

  it("pins the canonical type-scale values from DESIGN.md §3", () => {
    // 7-tier scale — locked so the scale cannot silently drift.
    expect(light).toContain("--text-xs: 11px");
    expect(light).toContain("--text-sm: 12px");
    expect(light).toContain("--text-base: 13px");
    expect(light).toContain("--text-md: 15px");
    expect(light).toContain("--text-lg: 19px");
    expect(light).toContain("--text-xl: 24px");
    expect(light).toContain("--text-2xl: 32px");
    // Line-height tiers
    expect(light).toContain("--leading-tight: 1.2");
    expect(light).toContain("--leading-snug: 1.45");
    expect(light).toContain("--leading-relaxed: 1.6");
    expect(light).toContain("--leading-prose: 1.7");
    // Letter-spacing tiers
    expect(light).toContain("--tracking-tight: -0.01em");
    expect(light).toContain("--tracking-eyebrow: 0.28em");
  });

  it("leaves no legacy font family names behind (§3 — Inter system)", () => {
    for (const family of LEGACY_FONTS) {
      expect(css).not.toContain(family);
    }
  });

  it("pins graph tokens to existing-token aliases (graph PRD §7, Rev 2)", () => {
    for (const block of [light, dark]) {
      // Node/edge derive from existing canvas/hairline/border-strong tokens —
      // no new color values may be introduced for the graph language.
      expect(block).toContain("--color-graph-node-fill: var(--color-canvas-elevated)");
      expect(block).toContain("--color-graph-node-fill-done: color-mix(in srgb, var(--color-brand) 18%, transparent)");
      expect(block).toContain("--color-graph-node-border: var(--color-hairline)");
      expect(block).toContain("--color-graph-node-border-active: var(--color-brand)");
      // Rev 2 §C.3: edges/arrows use border-strong, not hairline.
      expect(block).toContain("--color-graph-edge: var(--color-border-strong)");
      expect(block).toContain("--color-graph-arrow: var(--color-border-strong)");
      // Deliver pulse + active ring are brand-family color-mix tints.
      expect(block).toContain("--color-graph-deliver: var(--color-brand-bright)");
      expect(block).toContain(
        "--color-graph-active-ring: color-mix(in srgb, var(--color-brand) 30%, transparent)",
      );
      // Motion tokens (§7.2).
      expect(block).toContain("--dur-deliver: 600ms");
      expect(block).toContain("--ease-deliver: var(--ease-out)");
      expect(block).toContain("--dur-ring-pulse: 1200ms");
      expect(block).toContain("--dur-layout: 350ms");
    }
  });
});
