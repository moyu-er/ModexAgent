import { describe, expect, it, vi } from "vitest";
import type { SuggestionItem } from "../types/suggestion";
import {
  activeQuery,
  buildInsertion,
  filterSuggestions,
  handleSuggestKey,
} from "./CommandSuggest";

function item(name: string, category: SuggestionItem["category"], description?: string): SuggestionItem {
  return { name, category, description };
}

const ITEMS: SuggestionItem[] = [
  item("weather", "skill", "Get the weather"),
  item("github", "skill", "Search GitHub repos"),
  item("git-log", "skill", "Show git history"),
  item("continue", "command", "Continue the conversation"),
];

describe("activeQuery (trigger logic)", () => {
  it("returns null when input does not start with '/'", () => {
    expect(activeQuery("hello", 5)).toBeNull();
    expect(activeQuery("", 0)).toBeNull();
  });

  it("returns the partial command name (without leading '/') when in the first token", () => {
    expect(activeQuery("/wea", 4)).toBe("wea");
    expect(activeQuery("/", 1)).toBe("");
    expect(activeQuery("/git", 4)).toBe("git");
  });

  it("returns null once a space appears at/before the caret (args region)", () => {
    expect(activeQuery("/weather ", 9)).toBeNull();
    expect(activeQuery("/weather Shanghai", 17)).toBeNull();
  });

  it("stays open when the caret is before a later space (still typing the name)", () => {
    expect(activeQuery("/weather ", 3)).toBe("we");
  });
});

describe("filterSuggestions (case-insensitive prefix)", () => {
  it("returns all items for an empty query", () => {
    expect(filterSuggestions(ITEMS, "")).toEqual(ITEMS);
  });

  it("matches case-insensitively by prefix across categories", () => {
    expect(filterSuggestions(ITEMS, "git").map((s) => s.name)).toEqual(["github", "git-log"]);
    expect(filterSuggestions(ITEMS, "GIT").map((s) => s.name)).toEqual(["github", "git-log"]);
    expect(filterSuggestions(ITEMS, "wea").map((s) => s.name)).toEqual(["weather"]);
    expect(filterSuggestions(ITEMS, "con").map((s) => s.name)).toEqual(["continue"]);
  });

  it("returns [] when nothing matches", () => {
    expect(filterSuggestions(ITEMS, "xyz")).toEqual([]);
  });
});

describe("buildInsertion (replacement + caret)", () => {
  it("replaces the partial token with /name + trailing space and sets caret after the space", () => {
    const { input, caret } = buildInsertion("/wea", 4, "weather");
    expect(input).toBe("/weather ");
    expect(caret).toBe("/weather ".length);
  });

  it("preserves text after the caret", () => {
    const { input, caret } = buildInsertion("/gi", 3, "github");
    expect(input).toBe("/github ");
    expect(caret).toBe("/github ".length);
  });

  it("preserves trailing text that was after the caret (caret mid-token)", () => {
    const { input, caret } = buildInsertion("/weather Shanghai", 7, "weather");
    expect(input).toBe("/weather r Shanghai");
    expect(caret).toBe("/weather ".length);
  });
});

describe("handleSuggestKey (keyboard navigation)", () => {
  const state = (open: boolean, count = 4, active = 0) => ({ open, count, active });

  it("returns false (no interception) when the menu is closed", () => {
    const cb = { setActive: vi.fn(), choose: vi.fn(), close: vi.fn() };
    expect(handleSuggestKey("ArrowDown", state(false), cb)).toBe(false);
    expect(handleSuggestKey("Enter", state(false), cb)).toBe(false);
    expect(cb.setActive).not.toHaveBeenCalled();
    expect(cb.choose).not.toHaveBeenCalled();
  });

  it("ArrowDown wraps from last to first", () => {
    const cb = { setActive: vi.fn(), choose: vi.fn(), close: vi.fn() };
    expect(handleSuggestKey("ArrowDown", state(true, 4, 3), cb)).toBe(true);
    expect(cb.setActive).toHaveBeenCalledWith(0);
  });

  it("ArrowUp wraps from first to last", () => {
    const cb = { setActive: vi.fn(), choose: vi.fn(), close: vi.fn() };
    expect(handleSuggestKey("ArrowUp", state(true, 4, 0), cb)).toBe(true);
    expect(cb.setActive).toHaveBeenCalledWith(3);
  });

  it("Enter and Tab choose the active row", () => {
    for (const key of ["Enter", "Tab"]) {
      const cb = { setActive: vi.fn(), choose: vi.fn(), close: vi.fn() };
      expect(handleSuggestKey(key, state(true, 4, 1), cb)).toBe(true);
      expect(cb.choose).toHaveBeenCalledWith(1);
    }
  });

  it("Escape falls through (returns false) without destroying input", () => {
    const cb = { setActive: vi.fn(), choose: vi.fn(), close: vi.fn() };
    expect(handleSuggestKey("Escape", state(true), cb)).toBe(false);
    expect(cb.close).not.toHaveBeenCalled();
  });

  it("does not intercept unrelated keys", () => {
    const cb = { setActive: vi.fn(), choose: vi.fn(), close: vi.fn() };
    expect(handleSuggestKey("a", state(true), cb)).toBe(false);
    expect(handleSuggestKey("Backspace", state(true), cb)).toBe(false);
  });
});
