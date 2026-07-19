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
] as const;

/** Legacy emerald values that must not survive the retokening. */
const LEGACY_EMERALD = ["#059669", "#10b981", "#047857", "#34d399", "#065f46"];

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
    expect(dark).toContain("--color-brand: #2DD4A8");
    expect(light).toContain("--color-brand: #0D9488");
    expect(dark).toContain("--color-ember: #F5A524");
    expect(light).toContain("--color-ember: #B45309");
  });

  it("uses brand-tinted alpha hairlines in dark mode (§2.2)", () => {
    expect(dark).toContain("--color-hairline: rgba(45, 212, 168, 0.14)");
    expect(dark).toContain("--color-border-strong: rgba(45, 212, 168, 0.28)");
  });

  it("leaves no legacy emerald values behind", () => {
    for (const hex of LEGACY_EMERALD) {
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
});
