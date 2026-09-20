// useSessions — title rename + sessions_changed refresh (PA-02).
//
// Contract:
//  - renameSession persists via renameSessionTitle (ws+pool scoped) and
//    refreshes the list with the SAME fetchSessions path as everything else.
//  - Rename rejections propagate (the dialog keeps the user's text).
//  - onSessionsChanged refreshes the list only for the pod's own workspace
//    ("" = home matches scopeWs === ""); cross-workspace pings are ignored.
//  - Rapid refreshes are stale-guarded: a slow earlier response can never
//    overwrite a newer one (same-request epoch guard as pool switches).
//  - Draft sessions (uuid-prefix) never get a title PATCH — honest handling:
//    an unpersisted draft has no server record to rename.

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { renderHook, act, waitFor } from "@testing-library/react";
import { useSessions } from "./useSessions";
import {
  fetchSessions,
  renameSessionTitle,
  type PoolInfo,
} from "../lib/api";
import type { ConversationInfo } from "../types/events";

vi.mock("../lib/api", () => ({
  fetchSessions: vi.fn(),
  deleteConversation: vi.fn(),
  renameSessionTitle: vi.fn(),
}));

const pools: PoolInfo[] = [{ name: "main" }];

function session(overrides: Partial<ConversationInfo>): ConversationInfo {
  return {
    session_id: "abc123.main",
    agent_name: "main",
    pool: "main",
    parent_session_id: null,
    created_at: 1,
    updated_at: 1,
    ...overrides,
  };
}

// Deferred fetch promise so tests can resolve responses in a chosen order.
function deferred<T>(): { promise: Promise<T>; resolve: (v: T) => void } {
  let resolve!: (v: T) => void;
  const promise = new Promise<T>((r) => {
    resolve = r;
  });
  return { promise, resolve };
}

describe("useSessions rename + refresh (PA-02)", () => {
  beforeEach(() => {
    vi.mocked(fetchSessions).mockResolvedValue([
      session({ metadata: { title: "Old title" } }),
    ]);
    vi.mocked(renameSessionTitle).mockResolvedValue({ updated: true });
  });

  afterEach(() => {
    vi.clearAllMocks();
    localStorage.clear();
  });

  it("renameSession PATCHes with ws+pool scope and refreshes the list", async () => {
    const { result } = renderHook(() => useSessions({ ws: "/ws/a", pools }));
    await waitFor(() =>
      expect(result.current.sessions[0]?.metadata).toEqual({ title: "Old title" }),
    );

    vi.mocked(fetchSessions).mockResolvedValue([
      session({ metadata: { title: "New title" } }),
    ]);
    let saved: string | null = null;
    await act(async () => {
      saved = await result.current.renameSession("abc123.main", "New title");
    });

    expect(saved).toBe("New title");
    expect(renameSessionTitle).toHaveBeenCalledWith(
      "abc123.main",
      "New title",
      "/ws/a",
      "main",
    );
    await waitFor(() =>
      expect(result.current.sessions[0]?.metadata).toEqual({ title: "New title" }),
    );
  });

  it("promotes a sent hero draft and accepts its authoritative title", async () => {
    const { result } = renderHook(() => useSessions({ ws: "/ws/a", pools }));
    await waitFor(() => expect(result.current.sessions).toHaveLength(1));
    let prefix = "";
    act(() => { prefix = result.current.createDraftForSend("main"); });
    act(() => { result.current.onSent(prefix); });
    act(() => { result.current.handleSessionReady(prefix, `${prefix}.main`); });
    vi.mocked(fetchSessions).mockResolvedValue([session({ session_id: `${prefix}.main`, metadata: { title: "Server title" } })]);
    act(() => result.current.onSessionsChanged("/ws/a"));
    await waitFor(() => expect(result.current.sessions.find((s) => s.session_id === `${prefix}.main`)?.metadata?.title).toBe("Server title"));
    await act(async () => { await result.current.renameSession(`${prefix}.main`, "Manual title"); });
    expect(renameSessionTitle).toHaveBeenCalledWith(`${prefix}.main`, "Manual title", "/ws/a", "main");
  });

  it("propagates rename failures so the dialog can preserve the input", async () => {
    vi.mocked(renameSessionTitle).mockRejectedValue(
      new Error("API 404 Not Found: no such session"),
    );
    const { result } = renderHook(() => useSessions({ ws: "", pools }));
    await waitFor(() => expect(result.current.sessions).toHaveLength(1));

    await expect(
      act(async () => {
        await result.current.renameSession("abc123.main", "Nope");
      }),
    ).rejects.toThrow("404");
    // No refresh was triggered by the failure path.
    expect(fetchSessions).toHaveBeenCalledTimes(1);
  });

  it("keeps a new assistant's conversation and title outside the history filter", async () => {
    let trip: ConversationInfo | null = null;
    // The sidebar selection stays on the persisted "main" while the new
    // conversation is created for "trip" (single-selection semantics: the
    // draft's pool is whatever createDraftForSend receives).
    localStorage.setItem("modexbot_active_pool:/ws/a", "main");
    vi.mocked(fetchSessions).mockImplementation(async (_ws, pool) => {
      const records = [session({}), ...(trip ? [trip] : [])];
      return pool ? records.filter((record) => record.pool === pool) : records;
    });
    const { result } = renderHook(() => useSessions({ ws: "/ws/a", pools: [...pools, { name: "trip" }] }));
    await waitFor(() => expect(result.current.sessions).toHaveLength(1));
    let prefix = "";
    act(() => { prefix = result.current.createDraftForSend("trip"); result.current.onSent(prefix); });
    act(() => result.current.handleSessionReady(prefix, `${prefix}.trip`));
    trip = session({ session_id: `${prefix}.trip`, agent_name: "trip", pool: "trip", metadata: { title: "Trip plan" } });
    act(() => result.current.onSessionsChanged("/ws/a"));
    await waitFor(() => expect(result.current.sessions.find((record) => record.session_id === `${prefix}.trip`)?.metadata?.title).toBe("Trip plan"));
    expect(result.current.activePool).toBe("main");
    act(() => result.current.handlePoolChange("trip"));
    expect(result.current.selectedId).toBe(`${prefix}.trip`);
  });

  it("onSessionsChanged refreshes only for its own workspace (home pod)", async () => {
    const { result } = renderHook(() => useSessions({ ws: "", pools }));
    await waitFor(() => expect(result.current.sessions).toHaveLength(1));
    const callsBefore = vi.mocked(fetchSessions).mock.calls.length;

    vi.mocked(fetchSessions).mockResolvedValue([
      session({ metadata: { title: "Refreshed" } }),
    ]);
    act(() => {
      result.current.onSessionsChanged("/ws/other");
    });
    expect(vi.mocked(fetchSessions).mock.calls.length).toBe(callsBefore);

    act(() => {
      result.current.onSessionsChanged("");
    });
    expect(vi.mocked(fetchSessions).mock.calls.length).toBe(callsBefore + 1);
    await waitFor(() =>
      expect(result.current.sessions[0]?.metadata).toEqual({ title: "Refreshed" }),
    );
  });

  it("onSessionsChanged refreshes only for its own workspace (non-home pod)", async () => {
    const { result } = renderHook(() => useSessions({ ws: "/ws/a", pools }));
    await waitFor(() => expect(result.current.sessions).toHaveLength(1));
    const callsBefore = vi.mocked(fetchSessions).mock.calls.length;

    act(() => {
      result.current.onSessionsChanged("");
      result.current.onSessionsChanged("/ws/b");
    });
    expect(vi.mocked(fetchSessions).mock.calls.length).toBe(callsBefore);

    act(() => {
      result.current.onSessionsChanged("/ws/a");
    });
    expect(vi.mocked(fetchSessions).mock.calls.length).toBe(callsBefore + 1);
  });

  it("onSessionsChanged refreshes for the same workspace despite Windows representation drift", async () => {
    const { result } = renderHook(() => useSessions({ ws: "F:\\Tool\\Proj", pools }));
    await waitFor(() => expect(result.current.sessions).toHaveLength(1));
    const callsBefore = vi.mocked(fetchSessions).mock.calls.length;

    act(() => {
      // Server-resolved canonical form: different drive-letter case,
      // forward slashes, trailing separator — same workspace.
      result.current.onSessionsChanged("f:/tool/proj/");
      // A genuinely different workspace stays ignored.
      result.current.onSessionsChanged("F:/Tool/Other");
    });
    expect(vi.mocked(fetchSessions).mock.calls.length).toBe(callsBefore + 1);
  });

  it("stale refresh response never overwrites a newer one (same pool)", async () => {
    vi.mocked(fetchSessions).mockResolvedValue([]);
    const { result } = renderHook(() => useSessions({ ws: "", pools }));
    await waitFor(() => expect(result.current.sessions).toHaveLength(0));

    const first = deferred<ConversationInfo[]>();
    const second = deferred<ConversationInfo[]>();
    vi.mocked(fetchSessions)
      .mockReturnValueOnce(first.promise)
      .mockReturnValueOnce(second.promise);

    act(() => {
      result.current.onSessionsChanged("");
      result.current.onSessionsChanged("");
    });

    // Resolve in the WRONG order: the older request lands last.
    await act(async () => {
      second.resolve([session({ metadata: { title: "Newer" } })]);
    });
    await act(async () => {
      first.resolve([session({ metadata: { title: "Older" } })]);
    });

    expect(result.current.sessions[0]?.metadata).toEqual({ title: "Newer" });
  });

  it("does not PATCH a title for an unpersisted uuid-prefix draft", async () => {
    vi.mocked(fetchSessions).mockResolvedValue([]);
    const { result } = renderHook(() => useSessions({ ws: "", pools }));

    let draftId = "";
    act(() => {
      draftId = result.current.createDraftForSend("main");
    });
    await expect(async () => {
      await result.current.renameSession(draftId, "Draft title");
    }).rejects.toThrow();
    expect(renameSessionTitle).not.toHaveBeenCalled();
  });

  it("falls back to the preferred pool (PA-07) when the persisted choice is unavailable", async () => {
    localStorage.setItem("modexbot_active_pool:__home__", "ghost");
    const manyPools: PoolInfo[] = [{ name: "main" }, { name: "coder" }];
    vi.mocked(fetchSessions).mockResolvedValue([]);
    const { result } = renderHook(() =>
      useSessions({ ws: "", pools: manyPools, preferredPool: "coder" }),
    );
    await waitFor(() => expect(result.current.sessions).toHaveLength(0));
    // The invalid persisted pool was replaced by the user's preferred pool,
    // never by a first-item guess.
    await waitFor(() => expect(result.current.activePool).toBe("coder"));
  });

  it("stays unselected with no persisted choice and no preferred pool (no silent main/first)", async () => {
    const manyPools: PoolInfo[] = [{ name: "main" }, { name: "coder" }];
    vi.mocked(fetchSessions).mockResolvedValue([]);
    const { result } = renderHook(() =>
      useSessions({ ws: "", pools: manyPools }),
    );
    await waitFor(() => expect(result.current.sessions).toHaveLength(0));
    await waitFor(() => expect(result.current.activePool).toBe(""));
  });

  it("keeps the persisted valid choice even when a preferred pool exists", async () => {
    localStorage.setItem("modexbot_active_pool:__home__", "coder");
    const manyPools: PoolInfo[] = [{ name: "main" }, { name: "coder" }];
    vi.mocked(fetchSessions).mockResolvedValue([]);
    const { result } = renderHook(() =>
      useSessions({ ws: "", pools: manyPools, preferredPool: "main" }),
    );
    await waitFor(() => expect(result.current.sessions).toHaveLength(0));
    expect(result.current.activePool).toBe("coder");
  });
});
