import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, renderHook } from "@testing-library/react";
import { useCommandSuggestions } from "./useCommandSuggestions";
import * as skillCache from "../lib/skillCache";

vi.mock("../lib/skillCache", () => ({
  getPoolSkills: vi.fn(),
  refreshPoolSkills: vi.fn(),
  peekPoolSkills: vi.fn(),
  subscribeSkills: vi.fn(),
  SKILL_REFRESH_INTERVAL_MS: 60_000,
}));

const mockPeek = vi.mocked(skillCache.peekPoolSkills);
const mockSubscribe = vi.mocked(skillCache.subscribeSkills);
const mockGet = vi.mocked(skillCache.getPoolSkills);
const mockRefresh = vi.mocked(skillCache.refreshPoolSkills);

function makeSubscribeMock() {
  const cbs = new Set<() => void>();
  mockSubscribe.mockImplementation((cb: () => void) => {
    cbs.add(cb);
    return () => cbs.delete(cb);
  });
  return { notify: () => cbs.forEach((cb) => cb()) };
}

describe("useCommandSuggestions", () => {
  beforeEach(() => {
    mockPeek.mockReset();
    mockSubscribe.mockReset();
    mockGet.mockReset();
    mockRefresh.mockReset();
    mockGet.mockResolvedValue([]);
    mockRefresh.mockResolvedValue([]);
  });

  it("returns built-in commands when skill cache is empty", () => {
    makeSubscribeMock();
    mockPeek.mockReturnValue(null);

    const { result } = renderHook(() => useCommandSuggestions("default", "main"));
    expect(result.current).toHaveLength(1);
    expect(result.current[0]).toEqual({
      name: "continue",
      category: "command",
      description: "Continue the conversation without injecting a message",
    });
  });

  it("merges skills and built-in commands", () => {
    makeSubscribeMock();
    mockPeek.mockReturnValue([
      { name: "weather", source: "local", description: "Get the weather" },
      { name: "github", source: "global", origin: "user", description: "Search repos" },
    ]);

    const { result } = renderHook(() => useCommandSuggestions("default", "main"));
    expect(result.current).toHaveLength(3);
    expect(result.current.map((s) => s.category)).toEqual(["skill", "skill", "command"]);
    expect(result.current.map((s) => s.name)).toEqual(["weather", "github", "continue"]);
  });

  it("updates when the cache is populated (subscriber notified)", () => {
    const sub = makeSubscribeMock();
    mockPeek.mockReturnValue(null);

    const { result } = renderHook(() => useCommandSuggestions("default", "main"));
    expect(result.current).toHaveLength(1); // only /continue

    mockPeek.mockReturnValue([{ name: "weather", source: "local" }]);
    act(() => sub.notify());

    expect(result.current).toHaveLength(2);
    expect(result.current.map((s) => s.name)).toEqual(["weather", "continue"]);
  });
});
