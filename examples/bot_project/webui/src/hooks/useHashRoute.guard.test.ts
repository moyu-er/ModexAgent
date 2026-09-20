import { act, renderHook } from "@testing-library/react";
import { afterEach, expect, it } from "vitest";
import { useHashRoute } from "./useHashRoute";

afterEach(() => window.history.replaceState(null, "", "/"));

it("keeps the editor mounted until a browser navigation is accepted", () => {
  window.history.replaceState(null, "", "#/settings/assistants");
  const { result } = renderHook(() => useHashRoute());
  let resume: (() => void) | null = null;
  act(() => result.current.setNavigationGuard((accept) => { resume = accept; }));
  act(() => {
    window.history.replaceState(null, "", "#/graphs");
    window.dispatchEvent(new HashChangeEvent("hashchange"));
  });
  expect(result.current.route).toEqual({ kind: "settingsSection", section: "assistants" });
  expect(window.location.hash).toBe("#/settings/assistants");
  act(() => { if (resume) resume(); });
  expect(result.current.route).toEqual({ kind: "graphs" });
  expect(window.location.hash).toBe("#/graphs");
});

it("guards app navigation and releases the guard on editor exit", () => {
  const { result } = renderHook(() => useHashRoute());
  act(() => result.current.setNavigationGuard(() => {}));
  act(() => result.current.navigate("/settings"));
  expect(result.current.route.kind).toBe("chat");
  act(() => result.current.setNavigationGuard(null));
  act(() => result.current.navigate("/settings"));
  expect(result.current.route.kind).toBe("settings");
});
