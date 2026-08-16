import { describe, it, expect, beforeEach } from "vitest";
import { renderHook, act } from "@testing-library/react";
import {
  useWorkspaceTabs,
  computeTabLabels,
  pathBasename,
} from "./useWorkspaceTabs";

const HOME = "F:\\home\\bot_project";
const WS_A = "F:\\tool\\pythonProject\\ModexAgent";
const WS_B = "D:\\projects\\side-project";

function seed(hook: { current: ReturnType<typeof useWorkspaceTabs> }): void {
  // Trigger the home-resolution seed effect.
  expect(hook.current.ready).toBe(true);
}

describe("pathBasename", () => {
  it("returns the last segment for windows and posix paths", () => {
    expect(pathBasename("F:\\tool\\pythonProject\\ModexAgent")).toBe("ModexAgent");
    expect(pathBasename("/home/user/project")).toBe("project");
    expect(pathBasename("D:\\projects\\side-project\\")).toBe("side-project");
  });
});

describe("computeTabLabels", () => {
  it("uses basenames and marks the home tab for localization", () => {
    const labels = computeTabLabels(
      [
        { id: "__home__", path: HOME },
        { id: "a", path: WS_A },
      ],
      HOME,
    );
    expect(labels["__home__"]).toBe("__home__");
    expect(labels["a"]).toBe("ModexAgent");
  });

  it("disambiguates colliding basenames from DIFFERENT paths with parent dir", () => {
    const labels = computeTabLabels(
      [
        { id: "__home__", path: HOME },
        { id: "a", path: "F:\\work\\webui" },
        { id: "b", path: "D:\\play\\webui" },
      ],
      HOME,
    );
    expect(labels["a"]).toBe("work ▸ webui");
    expect(labels["b"]).toBe("play ▸ webui");
  });

  it("keeps identical labels for identical paths (no special-casing)", () => {
    const labels = computeTabLabels(
      [
        { id: "__home__", path: HOME },
        { id: "a", path: WS_A },
        { id: "b", path: WS_A },
      ],
      HOME,
    );
    expect(labels["a"]).toBe("ModexAgent");
    expect(labels["b"]).toBe("ModexAgent");
  });
});

describe("useWorkspaceTabs", () => {
  beforeEach(() => {
    sessionStorage.clear();
  });

  it("seeds with just the home tab when nothing is persisted", () => {
    const { result } = renderHook(() => useWorkspaceTabs(HOME));
    seed(result);
    expect(result.current.tabs).toEqual([{ id: "__home__", path: HOME }]);
    expect(result.current.activeId).toBe("__home__");
  });

  it("migrates the legacy single-workspace key into a second tab", () => {
    sessionStorage.setItem("modexbot_workspace", WS_A);
    const { result } = renderHook(() => useWorkspaceTabs(HOME));
    expect(result.current.tabs.map((t) => t.path)).toEqual([HOME, WS_A]);
    expect(result.current.activeId).toBe(result.current.tabs[1]!.id);
  });

  it("openWorkspace always appends and activates — even for an open path", () => {
    const { result } = renderHook(() => useWorkspaceTabs(HOME));
    act(() => result.current.openWorkspace(WS_A));
    act(() => result.current.openWorkspace(WS_A));
    expect(result.current.tabs).toHaveLength(3);
    expect(result.current.tabs[1]!.path).toBe(WS_A);
    expect(result.current.tabs[2]!.path).toBe(WS_A);
    expect(result.current.tabs[1]!.id).not.toBe(result.current.tabs[2]!.id);
    expect(result.current.activeId).toBe(result.current.tabs[2]!.id);
  });

  it("closeTab removes the tab and activates the left neighbor", () => {
    const { result } = renderHook(() => useWorkspaceTabs(HOME));
    act(() => result.current.openWorkspace(WS_A));
    act(() => result.current.openWorkspace(WS_B));
    const bId = result.current.activeId;
    act(() => result.current.closeTab(bId));
    expect(result.current.tabs.map((t) => t.path)).toEqual([HOME, WS_A]);
    expect(result.current.activeId).toBe(result.current.tabs[1]!.id);
  });

  it("closing an inactive tab keeps the active selection", () => {
    const { result } = renderHook(() => useWorkspaceTabs(HOME));
    act(() => result.current.openWorkspace(WS_A));
    act(() => result.current.openWorkspace(WS_B));
    const aId = result.current.tabs[1]!.id;
    const bId = result.current.tabs[2]!.id;
    act(() => result.current.closeTab(aId));
    expect(result.current.activeId).toBe(bId);
  });

  it("never closes the home tab", () => {
    const { result } = renderHook(() => useWorkspaceTabs(HOME));
    act(() => result.current.closeTab("__home__"));
    expect(result.current.tabs).toHaveLength(1);
  });

  it("reorderTab moves tabs but keeps home pinned at index 0", () => {
    const { result } = renderHook(() => useWorkspaceTabs(HOME));
    act(() => result.current.openWorkspace(WS_A));
    act(() => result.current.openWorkspace(WS_B));
    const bId = result.current.tabs[2]!.id;
    act(() => result.current.reorderTab(bId, 0)); // clamped to 1
    expect(result.current.tabs.map((t) => t.path)).toEqual([HOME, WS_B, WS_A]);
    act(() => result.current.reorderTab("__home__", 2)); // no-op
    expect(result.current.tabs[0]!.id).toBe("__home__");
  });

  it("persists and restores tabs + active across a remount", () => {
    const first = renderHook(() => useWorkspaceTabs(HOME));
    act(() => first.result.current.openWorkspace(WS_A));
    act(() => first.result.current.openWorkspace(WS_B));
    const aId = first.result.current.tabs[1]!.id;
    act(() => first.result.current.activateTab(aId));
    first.unmount();

    const second = renderHook(() => useWorkspaceTabs(HOME));
    expect(second.result.current.tabs.map((t) => t.path)).toEqual([HOME, WS_A, WS_B]);
    expect(second.result.current.activeId).toBe(aId);
  });

  it("reportStatus stores per-tab status and skips no-op writes", () => {
    const { result } = renderHook(() => useWorkspaceTabs(HOME));
    act(() => result.current.openWorkspace(WS_A));
    const id = result.current.tabs[1]!.id;
    const status = { running: 2, pendingApprovals: 1, connected: true };
    act(() => result.current.reportStatus(id, status));
    expect(result.current.statuses[id]).toEqual(status);
    const before = result.current.statuses;
    act(() => result.current.reportStatus(id, { ...status }));
    expect(result.current.statuses).toBe(before); // reference unchanged
  });

  it("reportStatus drops the entry when its tab closes", () => {
    const { result } = renderHook(() => useWorkspaceTabs(HOME));
    act(() => result.current.openWorkspace(WS_A));
    const id = result.current.tabs[1]!.id;
    act(() => result.current.reportStatus(id, { running: 1, pendingApprovals: 0, connected: true }));
    act(() => result.current.closeTab(id));
    expect(result.current.statuses[id]).toBeUndefined();
  });
});
