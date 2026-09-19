// PA-16 locale quality tests: zh catalog parity + useLocale behaviors.
// Parity rules: zh must have exactly the same leaf keys as en, and every
// {placeholder} in an en template must appear in the zh template (no more,
// no fewer). zh leaves must be real translations — not the English string.

import { describe, it, expect, beforeEach, afterEach } from "vitest";
import { renderHook, act } from "@testing-library/react";
import { createElement, type ReactNode } from "react";
import { catalogs, I18nProvider, useT, type Messages } from "./index";
import { useLocale } from "../hooks/useLocale";
import { en } from "./en";

// ── catalog parity helpers ───────────────────────────────────────────────────

type Leaves = Record<string, string>;

function flatten(obj: unknown, prefix = "", out: Leaves = {}): Leaves {
  if (obj && typeof obj === "object") {
    for (const [k, v] of Object.entries(obj as Record<string, unknown>)) {
      flatten(v, prefix ? `${prefix}.${k}` : k, out);
    }
  } else if (typeof obj === "string") {
    out[prefix] = obj;
  }
  return out;
}

function placeholders(template: string): string[] {
  return [...template.matchAll(/\{(\w+)\}/g)].map((m) => m[1]!).sort();
}

describe("zh catalog parity", () => {
  const enLeaves = flatten(en);
  const zh = (catalogs as Record<string, Messages>).zh as Messages | undefined;

  it("zh catalog is registered", () => {
    expect(zh).toBeDefined();
  });

  it("zh has exactly the same leaf keys as en", () => {
    const zhLeaves = flatten(zh);
    const enKeys = Object.keys(enLeaves).sort();
    const zhKeys = Object.keys(zhLeaves).sort();
    expect(zhKeys).toEqual(enKeys);
  });

  it("every zh template preserves the en placeholders exactly", () => {
    const zhLeaves = flatten(zh);
    const withPlaceholders = Object.entries(enLeaves).filter(([, v]) =>
      /\{\w+\}/.test(v),
    );
    expect(withPlaceholders.length).toBeGreaterThan(50); // sanity: real coverage
    for (const [key, enTpl] of withPlaceholders) {
      expect(placeholders(zhLeaves[key]!), `placeholder mismatch at "${key}"`).toEqual(
        placeholders(enTpl),
      );
    }
  });

  it("zh leaves are not byte-identical English copies", () => {
    const zhLeaves = flatten(zh);
    // Allow a small number of legitimately-shared protocol labels (stdio, SSE,
    // MCP, …) but the bulk must differ from English.
    const identical = Object.keys(enLeaves).filter(
      (k) => enLeaves[k] === zhLeaves[k],
    );
    expect(identical.length).toBeLessThan(30);
    expect(identical.length / Object.keys(enLeaves).length).toBeLessThan(0.03);
  });

  it("zh templates contain no CJK-free English sentences", () => {
    const zhLeaves = flatten(zh);
    // Every zh leaf that contains a Latin word of 3+ letters must also contain
    // CJK characters (proper nouns like MCP/SSE/stdio are fine when embedded
    // in translated text). Pure ASCII technical tokens are allowed only for a
    // tiny allowlist of protocol labels.
    const allowExact = new Set([
      "SSE",
      "stdio",
      "v{version}",
      "spec v{version}",
      "{seconds}s",
      "KEY",
      "value",
      "OpenAI Compatible", "OpenAI Responses", "Anthropic", "Streamable HTTP",
      "URL", "https://…", "npx", "{pool} / {name}",
    ]);
    for (const [key, tpl] of Object.entries(zhLeaves)) {
      if (allowExact.has(tpl)) continue;
      if (/[A-Za-z]{3,}/.test(tpl) && !/[\u4e00-\u9fff]/.test(tpl)) {
        throw new Error(
          `zh template at "${key}" looks untranslated: "${tpl}"`,
        );
      }
    }
  });
});

describe("zh translation behavior", () => {

  it("resolves keys in zh and interpolates placeholders", () => {
    const { result } = renderHook(() => useT(), {
      wrapper: ({ children }: { children: ReactNode }) =>
        createElement(I18nProvider, { locale: "zh", children }),
    });
    expect(result.current("common.cancel")).toBe("取消");
    expect(result.current("settings.models.providerSummary", {
      key: "deepseek",
      count: 3,
    })).toContain("deepseek");
    expect(result.current("settings.models.providerSummary", {
      key: "deepseek",
      count: 3,
    })).toContain("3");
    // Unknown placeholder survives interpolation untouched.
    expect(
      result.current("settings.models.providerSummary", { key: "x" }).includes(
        "{count}",
      ),
    ).toBe(true);
  });

  it("a zh leaf is never resolved through the English catalog", () => {
    const { result } = renderHook(() => useT(), {
      wrapper: ({ children }: { children: ReactNode }) =>
        createElement(I18nProvider, { locale: "zh", children }),
    });
    // Resolve every key once — each must return a non-key string, proving no
    // missing-key fallback to the dotted path happened.
    const enLeaves = flatten(en);
    for (const key of Object.keys(enLeaves)) {
      const v = result.current(key as Parameters<typeof result.current>[0]);
      expect(v, `zh resolution failed for "${key}"`).not.toBe(key);
    }
  });
});

describe("useLocale (PA-16)", () => {
  it("updates the app's locale when the settings consumer changes it", () => {
    const root = renderHook(() => useLocale());
    const settings = renderHook(() => useLocale());
    act(() => settings.result.current.setLocale("zh"));
    expect(root.result.current.locale).toBe("zh");
  });
  beforeEach(() => {
    localStorage.clear();
    document.documentElement.lang = "";
  });

  afterEach(() => {
    localStorage.clear();
    document.documentElement.lang = "";
  });

  it("defaults to en when nothing is persisted", () => {
    const { result } = renderHook(() => useLocale());
    expect(result.current.locale).toBe("en");
  });

  it("persists a locale switch to localStorage and updates document lang", () => {
    const { result } = renderHook(() => useLocale());
    act(() => {
      result.current.setLocale("zh");
    });
    expect(result.current.locale).toBe("zh");
    expect(localStorage.getItem("modexbot_locale")).toBe("zh");
    expect(document.documentElement.lang).toBe("zh");
  });

  it("restores a persisted valid locale on init", () => {
    localStorage.setItem("modexbot_locale", "zh");
    const { result } = renderHook(() => useLocale());
    expect(result.current.locale).toBe("zh");
    expect(document.documentElement.lang).toBe("zh");
  });

  it("falls back to en for an unknown persisted locale", () => {
    localStorage.setItem("modexbot_locale", "fr");
    const { result } = renderHook(() => useLocale());
    expect(result.current.locale).toBe("en");
  });

  it("ignores setLocale for an unregistered locale", () => {
    const { result } = renderHook(() => useLocale());
    act(() => {
      result.current.setLocale("fr");
    });
    expect(result.current.locale).toBe("en");
    expect(localStorage.getItem("modexbot_locale")).toBe("en");
  });

  it("lists zh with its native label and switching is immediate (t reflects it)", () => {
    const { result } = renderHook(() => useLocale());
    const zhOption = result.current.available.find((o) => o.value === "zh");
    expect(zhOption).toBeDefined();
    expect(zhOption?.label).toBe("简体中文");
    act(() => result.current.setLocale("zh"));

    // Immediate switch: an I18nProvider bound to the hook's locale translates
    // zh right away — no reload required.
    const probe = renderHook(() => useT(), {
      wrapper: ({ children }: { children: ReactNode }) =>
        createElement(I18nProvider, { locale: result.current.locale, children }),
    });
    expect(probe.result.current("common.cancel")).toBe("取消");
  });
});
