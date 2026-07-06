import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, waitFor, cleanup, fireEvent } from "@testing-library/react";
import { changeWorkspace } from "../lib/api";
import { Sidebar } from "./Sidebar";
import { ToastProvider } from "./ToastContext";

vi.mock("../lib/api", () => ({
  changeWorkspace: vi.fn(),
}));

const noop = (): void => {};

describe("Sidebar recent workspace click", () => {
  beforeEach(() => {
    vi.mocked(changeWorkspace).mockReset();
  });

  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
  });

  function renderSidebar(props: Partial<React.ComponentProps<typeof Sidebar>> = {}) {
    return render(
      <ToastProvider>
        <Sidebar
          sessionTree={[]}
          pools={[{ name: "main" }]}
          selected={null}
          workspace="/home"
          isHome
          activePool="main"
          recentWorkspaces={[{ path: "/ws_a" }, { path: "/ws_b" }]}
          mobileOpen={false}
          onCloseMobile={noop}
          onSelect={noop}
          onNew={noop}
          onDelete={noop}
          onWorkspaceChanged={noop}
          onGoHome={noop}
          onPoolChange={noop}
          {...props}
        />
      </ToastProvider>,
    );
  }

  it("coerces a non-string cwd from changeWorkspace before calling onWorkspaceChanged", async () => {
    const onWorkspaceChanged = vi.fn();
    vi.mocked(changeWorkspace).mockResolvedValue({
      success: true,
      // Simulate a backend serialization quirk where cwd becomes an object.
      cwd: { toString: () => "/ws_obj" } as unknown as string,
      notice: "",
    });

    const { container } = renderSidebar({ onWorkspaceChanged });

    // Debug: dump button texts.
    // eslint-disable-next-line no-console
    console.log("BUTTONS:", Array.from(container.querySelectorAll("button")).map((b) => b.textContent));

    // Open the Recent dropdown.
    const buttons = Array.from(container.querySelectorAll("button"));
    const recentToggle = buttons.find((b) => b.textContent?.includes("Recent"));
    if (!recentToggle) {
      throw new Error(
        `No Recent button. Buttons: ${buttons.map((b) => JSON.stringify(b.textContent)).join(", ")}\nHTML: ${container.innerHTML.slice(0, 1000)}`,
      );
    }
    fireEvent.click(recentToggle);

    // Click the first recent item.
    await waitFor(() => {
      expect(document.body.textContent).toContain("/ws_a");
    });
    const item = Array.from(container.querySelectorAll("button")).find(
      (b) => b.textContent?.includes("/ws_a"),
    );
    expect(item).toBeTruthy();
    fireEvent.click(item!);

    await waitFor(() => {
      expect(onWorkspaceChanged).toHaveBeenCalledWith("/ws_obj");
    });
  });

  it("renders object-like path entries without crashing", async () => {
    const onWorkspaceChanged = vi.fn();
    vi.mocked(changeWorkspace).mockResolvedValue({
      success: true,
      cwd: "/ws_a",
      notice: "",
    });

    // Cast to silence the type checker; runtime is what matters here.
    const recentWithObjectPath = [
      { path: "/ws_a" },
      { path: { toString: () => "/ws_b" } },
    ] as { path: string }[];

    const { container } = renderSidebar({
      onWorkspaceChanged,
      recentWorkspaces: recentWithObjectPath,
    });

    const recentToggle = Array.from(container.querySelectorAll("button")).find(
      (b) => b.textContent?.includes("Recent"),
    );
    fireEvent.click(recentToggle!);

    // Both entries should be rendered (object path coerced to string).
    await waitFor(() => {
      expect(document.body.textContent).toContain("/ws_a");
      expect(document.body.textContent).toContain("/ws_b");
    });

    // Clicking the object-path entry should still work.
    const item = Array.from(container.querySelectorAll("button")).find(
      (b) => b.textContent?.includes("/ws_b"),
    );
    expect(item).toBeTruthy();
    fireEvent.click(item!);

    await waitFor(() => {
      expect(changeWorkspace).toHaveBeenCalledWith("/ws_b");
    });
  });
});
