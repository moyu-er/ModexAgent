// Restart indicator: a restart_required save arms `toast.restart.setRestartNeeded(true)`,
// which renders a red dot on the tab bar's settings gear. Tested at the seam
// (ToastContext + WorkspaceTabBar) rather than via a full save flow, since the
// indicator's contract is just "any setRestartNeeded(true) → dot appears".

import { describe, it, expect, afterEach, vi } from "vitest";
import { render, screen, act, cleanup } from "@testing-library/react";
import { ToastProvider, useToast } from "../components/ToastContext";
import { WorkspaceTabBar } from "../components/WorkspaceTabBar";

vi.mock("../lib/api", () => ({ changeWorkspace: vi.fn() }));

const noop = (): void => {};

function Harness() {
  const { restart } = useToast();
  return (
    <div>
      <button
        type="button"
        onClick={() => restart.setRestartNeeded(true)}
        data-testid="arm"
      >
        arm
      </button>
      <WorkspaceTabBar
        tabs={[{ id: "__home__", path: "/home" }]}
        activeId="__home__"
        statuses={{}}
        home="/home"
        recentWorkspaces={[]}
        onOpenWorkspace={noop}
        onOpenRecent={noop}
        onActivate={noop}
        onClose={noop}
        onReorder={noop}
        onOpenSettings={noop}
      />
    </div>
  );
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("restart indicator", () => {
  it("gear shows a red dot after setRestartNeeded(true)", () => {
    render(
      <ToastProvider>
        <Harness />
      </ToastProvider>,
    );
    // no dot before arming
    expect(screen.queryByLabelText("Restart required")).toBeNull();
    act(() => {
      screen.getByTestId("arm").click();
    });
    expect(screen.getByLabelText("Restart required")).toBeTruthy();
  });
});
