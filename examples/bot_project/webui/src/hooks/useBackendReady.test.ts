import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, renderHook } from "@testing-library/react";
import { useBackendReady } from "./useBackendReady";
import { bootStageKey } from "../components/BootScreen";
import { fetchPools } from "../lib/api";

vi.mock("../lib/api", () => ({ fetchPools: vi.fn() }));
const mockFetchPools = vi.mocked(fetchPools);

describe("bootStageKey (staged status copy)", () => {
  it("maps attempt counts to the staged boot messages", () => {
    expect(bootStageKey(1)).toBe("boot.starting");
    expect(bootStageKey(2)).toBe("boot.connecting");
    expect(bootStageKey(10)).toBe("boot.connecting");
    expect(bootStageKey(11)).toBe("boot.stillStarting");
    expect(bootStageKey(30)).toBe("boot.stillStarting");
    expect(bootStageKey(31)).toBe("boot.takingLong");
  });
});

describe("useBackendReady", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    mockFetchPools.mockReset();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("flips ready once the backend responds", async () => {
    mockFetchPools.mockResolvedValue([]);
    const { result } = renderHook(() => useBackendReady());
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(result.current.ready).toBe(true);
    expect(result.current.attempts).toBe(1);
    expect(result.current.lastError).toBeNull();
  });

  it("keeps polling on failure, recording attempts and the raw error", async () => {
    mockFetchPools.mockRejectedValue(new Error("connection refused"));
    const { result } = renderHook(() => useBackendReady());
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(result.current.attempts).toBe(1);
    expect(result.current.lastError).toBe("connection refused");
    expect(result.current.ready).toBe(false);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(2000);
    });
    expect(result.current.attempts).toBe(3);
    expect(mockFetchPools).toHaveBeenCalledTimes(3);
  });

  it("retry() resets attempts/error and resumes polling", async () => {
    mockFetchPools.mockRejectedValue(new Error("down"));
    const { result } = renderHook(() => useBackendReady());
    await act(async () => {
      await vi.advanceTimersByTimeAsync(3000);
    });
    expect(result.current.attempts).toBe(4);
    expect(result.current.lastError).toBe("down");

    mockFetchPools.mockResolvedValue([]);
    act(() => result.current.retry());
    expect(result.current.lastError).toBeNull();
    expect(result.current.ready).toBe(false);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(result.current.ready).toBe(true);
    // Attempt counter restarted from 1 after the retry.
    expect(result.current.attempts).toBe(1);
  });

  it("stops polling after the max attempts", async () => {
    mockFetchPools.mockRejectedValue(new Error("down"));
    renderHook(() => useBackendReady());
    await act(async () => {
      await vi.advanceTimersByTimeAsync(130000);
    });
    expect(mockFetchPools).toHaveBeenCalledTimes(120);
  });
});
