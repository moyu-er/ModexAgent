import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, renderHook } from "@testing-library/react";
import { usePoolSkills, usePoolSkillsWarmup } from "./usePoolSkills";
import * as skillCache from "../lib/skillCache";
import type { SkillEntry } from "../types/pool";

vi.mock("../lib/skillCache", () => ({
  getPoolSkills: vi.fn(),
  refreshPoolSkills: vi.fn(),
  peekPoolSkills: vi.fn(),
  subscribeSkills: vi.fn(),
  SKILL_REFRESH_INTERVAL_MS: 60_000,
}));

const mockGet = vi.mocked(skillCache.getPoolSkills);
const mockRefresh = vi.mocked(skillCache.refreshPoolSkills);
const mockPeek = vi.mocked(skillCache.peekPoolSkills);
const mockSubscribe = vi.mocked(skillCache.subscribeSkills);

function skill(name: string): SkillEntry {
  return { name, source: "local" };
}

// useSyncExternalStore needs subscribe to return an unsubscribe and to call
// the callback on changes. We wire a minimal stand-in.
function makeSubscribeMock() {
  const cbs = new Set<() => void>();
  mockSubscribe.mockImplementation((cb: () => void) => {
    cbs.add(cb);
    return () => cbs.delete(cb);
  });
  return {
    notify: () => cbs.forEach((cb) => cb()),
  };
}

describe("usePoolSkills (read-only)", () => {
  beforeEach(() => {
    mockPeek.mockReset();
    mockSubscribe.mockReset();
    mockGet.mockReset();
    mockRefresh.mockReset();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("returns [] when pool or mainAgent is undefined", () => {
    makeSubscribeMock();
    mockPeek.mockReturnValue(null);
    const { result: r1 } = renderHook(() => usePoolSkills(undefined, "main"));
    expect(r1.current).toEqual([]);
    const { result: r2 } = renderHook(() => usePoolSkills("default", undefined));
    expect(r2.current).toEqual([]);
  });

  it("returns the cached value from peekPoolSkills without fetching", () => {
    makeSubscribeMock();
    const cached = [skill("weather"), skill("github")];
    mockPeek.mockReturnValue(cached);

    const { result } = renderHook(() => usePoolSkills("default", "main"));
    expect(result.current).toEqual(cached);
    expect(mockGet).not.toHaveBeenCalled();
    expect(mockRefresh).not.toHaveBeenCalled();
  });

  it("returns [] when the cache is empty (no suggestions until warmup)", () => {
    makeSubscribeMock();
    mockPeek.mockReturnValue(null);

    const { result } = renderHook(() => usePoolSkills("default", "main"));
    expect(result.current).toEqual([]);
    expect(mockGet).not.toHaveBeenCalled();
  });

  it("re-renders with the new value when the cache is updated (subscriber notified)", () => {
    const sub = makeSubscribeMock();
    mockPeek.mockReturnValue(null);

    const { result } = renderHook(() => usePoolSkills("default", "main"));
    expect(result.current).toEqual([]);

    // Simulate the warmup completing: cache now holds data, subscribers fire.
    const data = [skill("weather")];
    mockPeek.mockReturnValue(data);
    act(() => sub.notify());

    expect(result.current).toEqual(data);
  });
});

describe("usePoolSkillsWarmup (fetch + refresh)", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    mockGet.mockReset();
    mockRefresh.mockReset();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("does nothing when pool or mainAgent is undefined", () => {
    renderHook(() => usePoolSkillsWarmup(undefined, "main"));
    expect(mockGet).not.toHaveBeenCalled();
    renderHook(() => usePoolSkillsWarmup("default", undefined));
    expect(mockGet).not.toHaveBeenCalled();
  });

  it("fetches via getPoolSkills on mount", async () => {
    mockGet.mockResolvedValue([skill("weather")]);

    renderHook(() => usePoolSkillsWarmup("default", "main"));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(mockGet).toHaveBeenCalledWith("default", "main");
  });

  it("refreshes via refreshPoolSkills on the 60s interval", async () => {
    mockGet.mockResolvedValue([skill("v1")]);
    mockRefresh.mockResolvedValue([skill("v1"), skill("v2")]);

    renderHook(() => usePoolSkillsWarmup("default", "main"));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(mockRefresh).not.toHaveBeenCalled();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(60_000);
    });
    expect(mockRefresh).toHaveBeenCalledWith("default", "main");
  });

  it("does not refresh before 60s", async () => {
    mockGet.mockResolvedValue([skill("v1")]);
    mockRefresh.mockResolvedValue([skill("v1")]);

    renderHook(() => usePoolSkillsWarmup("default", "main"));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(59_999);
    });
    expect(mockRefresh).not.toHaveBeenCalled();
  });

  it("clears the interval on unmount", async () => {
    mockGet.mockResolvedValue([skill("v1")]);
    mockRefresh.mockResolvedValue([skill("v1")]);

    const { unmount } = renderHook(() => usePoolSkillsWarmup("default", "main"));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    unmount();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(120_000);
    });
    expect(mockRefresh).not.toHaveBeenCalled();
  });

  it("re-fetches when the (pool, mainAgent) pair changes", async () => {
    mockGet.mockResolvedValue([skill("a")]);

    const { rerender } = renderHook(
      ({ p, a }) => usePoolSkillsWarmup(p, a),
      { initialProps: { p: "default", a: "main" } },
    );
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(mockGet).toHaveBeenCalledWith("default", "main");

    rerender({ p: "coder", a: "orchestrator" });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(mockGet).toHaveBeenLastCalledWith("coder", "orchestrator");
  });

  it("swallows fetch errors silently (cache stays empty, retry on next tick)", async () => {
    mockGet.mockRejectedValue(new Error("network down"));
    mockRefresh.mockResolvedValue([skill("v1")]);

    renderHook(() => usePoolSkillsWarmup("default", "main"));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(60_000);
    });
    expect(mockRefresh).toHaveBeenCalledWith("default", "main");
  });
});
