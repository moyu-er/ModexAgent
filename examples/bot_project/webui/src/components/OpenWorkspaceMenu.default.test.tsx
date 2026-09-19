// OpenWorkspaceMenu (PA-08): the pin action saves the default-workspace
// preference through the host callback; the default entry shows a pressed
// pin; opening still dispatches the path.

import { describe, it, expect, vi, afterEach } from "vitest";
import { render, waitFor, cleanup } from "@testing-library/react";
import { OpenWorkspaceMenu } from "./OpenWorkspaceMenu";
import { ToastProvider } from "./ToastContext";

function screen_pin(): HTMLButtonElement {
  const el = document.querySelector<HTMLButtonElement>(".wsopen-item-side");
  expect(el).toBeTruthy();
  return el!;
}

vi.mock("./lib/api", () => ({
  changeWorkspace: vi.fn(),
  pickWorkspace: vi.fn(),
}));

const noop = (): void => {};

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

function renderMenu(
  props: Partial<React.ComponentProps<typeof OpenWorkspaceMenu>> = {},
) {
  return render(
    <ToastProvider>
      <OpenWorkspaceMenu
        open
        onClose={noop}
        recentWorkspaces={[{ path: "/ws_a" }, { path: "/ws_b" }]}
        defaultWorkspace="/ws_a"
        onOpenRecent={noop}
        onBrowsePicked={noop}
        onSetDefault={noop}
        onGoHome={noop}
        {...props}
      />
    </ToastProvider>,
  );
}

describe("OpenWorkspaceMenu set-default (PA-08)", () => {
  it("the default entry's pin is aria-pressed", async () => {
    renderMenu();
    const pin = await waitFor(() =>
      screen_pin(),
    );
    expect(pin.getAttribute("aria-pressed")).toBe("true");
  });

  it("clicking a pin saves that path as the default (not open, no cd)", async () => {
    const onSetDefault = vi.fn();
    const onOpenRecent = vi.fn();
    renderMenu({ onSetDefault, onOpenRecent });
    const pins = document.querySelectorAll<HTMLButtonElement>(".wsopen-item-side");
    expect(pins).toHaveLength(2);
    pins[1]!.click();
    expect(onSetDefault).toHaveBeenCalledWith("/ws_b");
    // Pinning never opens a workspace.
    expect(onOpenRecent).not.toHaveBeenCalled();
    // The menu stays open (the user may keep browsing).
    expect(document.querySelector(".wsopen-menu")).toBeTruthy();
  });
});
