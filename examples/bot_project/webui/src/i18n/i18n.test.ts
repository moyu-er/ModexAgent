import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { renderHook, act } from "@testing-library/react";
import { createElement, type ReactNode } from "react";
import {
  I18nProvider,
  useT,
  en,
  type MessageKey,
} from "./index";

// A key known to exist in the catalog for resolution checks.
const HELLO_KEY = "common.cancel" as const satisfies MessageKey;
const INTERP_KEY = "settings.models.providerSummary" as const satisfies MessageKey;

describe("i18n", () => {
  describe("key resolution", () => {
    it("resolves a dotted key to its English string", () => {
      const { result } = renderHook(() => useT(), {
        wrapper: ({ children }: { children: ReactNode }) =>
          createElement(I18nProvider, null, children),
      });
      expect(result.current(HELLO_KEY)).toBe("Cancel");
    });

    it("resolves a nested key through multiple levels", () => {
      const { result } = renderHook(() => useT(), {
        wrapper: ({ children }: { children: ReactNode }) =>
          createElement(I18nProvider, null, children),
      });
      expect(result.current("settings.models.defaultModel")).toBe(
        "Default model",
      );
      expect(result.current("toast.savedRestart")).toBe(
        "Saved. Restart to apply.",
      );
    });

    it("resolves keys without a provider using the default English t()", () => {
      // Render without any wrapper — the default context value is the
      // English translator, so existing component tests that never mount
      // I18nProvider still get correct English strings.
      const { result } = renderHook(() => useT());
      expect(result.current(HELLO_KEY)).toBe("Cancel");
      expect(result.current("settings.pools.title")).toBe("Pools");
    });
  });

  describe("interpolation", () => {
    it("replaces {name} placeholders with param values", () => {
      const { result } = renderHook(() => useT(), {
        wrapper: ({ children }: { children: ReactNode }) =>
          createElement(I18nProvider, null, children),
      });
      expect(
        result.current(INTERP_KEY, { key: "deepseek", count: 3 }),
      ).toBe("key: deepseek · 3 model(s)");
    });

    it("handles numeric params", () => {
      const { result } = renderHook(() => useT(), {
        wrapper: ({ children }: { children: ReactNode }) =>
          createElement(I18nProvider, null, children),
      });
      expect(
        result.current("settings.modelsFetch.importSelected", { count: 5 }),
      ).toBe("Import selected (5)");
    });

    it("leaves unknown placeholders as {name} when param is missing", () => {
      const { result } = renderHook(() => useT(), {
        wrapper: ({ children }: { children: ReactNode }) =>
          createElement(I18nProvider, null, children),
      });
      // Only provide one of two placeholders.
      expect(result.current(INTERP_KEY, { key: "x" })).toBe(
        "key: x · {count} model(s)",
      );
    });

    it("returns the plain template when no params are given", () => {
      const { result } = renderHook(() => useT(), {
        wrapper: ({ children }: { children: ReactNode }) =>
          createElement(I18nProvider, null, children),
      });
      expect(result.current("settings.pools.discardSwitchMessage")).toBe(
        "Switching pools now will lose your edits to the current pool.",
      );
    });
  });

  describe("missing-key fallback", () => {
    let warnSpy: ReturnType<typeof vi.spyOn>;

    beforeEach(() => {
      warnSpy = vi.spyOn(console, "warn").mockImplementation(() => {});
    });

    afterEach(() => {
      warnSpy.mockRestore();
    });

    it("returns the key string and warns once for a missing key", () => {
      const { result } = renderHook(() => useT(), {
        wrapper: ({ children }: { children: ReactNode }) =>
          createElement(I18nProvider, null, children),
      });
      const badKey = "common.thisDoesNotExist" as MessageKey;
      expect(result.current(badKey)).toBe("common.thisDoesNotExist");
      expect(warnSpy).toHaveBeenCalledTimes(1);
      // Second call with the same key should NOT warn again.
      act(() => {
        result.current(badKey);
      });
      expect(warnSpy).toHaveBeenCalledTimes(1);
    });

    it("returns the key for a completely unknown top-level namespace", () => {
      const { result } = renderHook(() => useT());
      const badKey = "nonexistent.nested.key" as MessageKey;
      expect(result.current(badKey)).toBe("nonexistent.nested.key");
    });
  });

  describe("provider locale switching", () => {
    it("falls back to English when an unknown locale is requested", () => {
      const { result } = renderHook(() => useT(), {
        wrapper: ({ children }: { children: ReactNode }) =>
          createElement(
            I18nProvider,
            { locale: "fr", children } as {
              locale: string;
              children: ReactNode;
            },
          ),
      });
      expect(result.current(HELLO_KEY)).toBe("Cancel");
    });
  });

  describe("catalog integrity", () => {
    it("en catalog has the expected top-level namespaces", () => {
      expect(en.common).toBeDefined();
      expect(en.settings).toBeDefined();
      expect(en.ui).toBeDefined();
      expect(en.toast).toBeDefined();
    });
  });
});
