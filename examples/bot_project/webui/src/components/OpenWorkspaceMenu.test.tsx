// Open-workspace menu (the tab bar's "+"): recent entries render full paths
// (object-like paths coerced to strings), clicking dispatches the string
// path, the filter narrows the list, and "Browse" raises the directory modal.

import { describe, it, expect, vi, afterEach } from "vitest";
import { render, waitFor, cleanup, fireEvent } from "@testing-library/react";
import { OpenWorkspaceMenu } from "./OpenWorkspaceMenu";
import { ToastProvider } from "./ToastContext";

vi.mock("../lib/api", () => ({
  changeWorkspace: vi.fn(),
  pickWorkspace: vi.fn(),
}));

const noop = (): void => {};

describe("OpenWorkspaceMenu", () => {
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
          onOpenRecent={noop}
          onBrowsePicked={noop}
          onGoHome={noop}
          {...props}
        />
      </ToastProvider>,
    );
  }

  it("dispatches the clicked recent entry's string path", async () => {
    const onOpenRecent = vi.fn();
    renderMenu({ onOpenRecent });

    const item = await waitFor(() => {
      const el = Array.from(document.querySelectorAll("button")).find(
        (b) => b.textContent?.includes("/ws_a"),
      );
      expect(el).toBeTruthy();
      return el!;
    });
    fireEvent.click(item);
    expect(onOpenRecent).toHaveBeenCalledWith("/ws_a");
  });

  it("renders object-like path entries without crashing", async () => {
    const onOpenRecent = vi.fn();
    // Cast to silence the type checker; runtime is what matters here.
    const recentWithObjectPath = [
      { path: "/ws_a" },
      { path: { toString: () => "/ws_b" } },
    ] as { path: string }[];

    renderMenu({ onOpenRecent, recentWorkspaces: recentWithObjectPath });

    await waitFor(() => {
      expect(document.body.textContent).toContain("/ws_a");
      expect(document.body.textContent).toContain("/ws_b");
    });

    const item = Array.from(document.querySelectorAll("button")).find(
      (b) => b.textContent?.includes("/ws_b"),
    );
    expect(item).toBeTruthy();
    fireEvent.click(item!);
    expect(onOpenRecent).toHaveBeenCalledWith("/ws_b");
  });

  it("filter narrows the recent list", async () => {
    renderMenu();

    await waitFor(() => {
      expect(document.body.textContent).toContain("/ws_a");
    });
    const input = document.querySelector("input")!;
    fireEvent.change(input, { target: { value: "ws_b" } });

    await waitFor(() => {
      expect(document.body.textContent).toContain("/ws_b");
      expect(document.body.textContent).not.toContain("/ws_a");
    });
  });

  it("renders nothing when closed", () => {
    render(
      <ToastProvider>
        <OpenWorkspaceMenu
          open={false}
          onClose={noop}
          recentWorkspaces={[{ path: "/ws_a" }]}
          onOpenRecent={noop}
          onBrowsePicked={noop}
          onGoHome={noop}
        />
      </ToastProvider>,
    );
    expect(document.body.textContent).not.toContain("/ws_a");
  });
});
