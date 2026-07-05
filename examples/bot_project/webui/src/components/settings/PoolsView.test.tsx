import { describe, it, expect, vi, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { PoolsView } from "./PoolsView";
import { ToastProvider } from "../ToastContext";

function makeResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

const pools = [
  { name: "default", main_agent_name: "main", subagent_count: 0 },
  { name: "research", main_agent_name: "main", subagent_count: 2 },
];

const tree = (name: string) => ({
  name,
  main_agent_name: "main",
  main: {
    agent_name: "main",
    max_steps: 12,
    use_terminal: false,
    terminal_visibility: false,
    tool_preset: "full",
    tool_supplements: [],
    approval: { enabled: false, tools: {} },
    mcp: [],
  },
  subagents: [],
  restart_required: false,
});

afterEach(() => vi.unstubAllGlobals());

function renderView(): void {
  render(
    <ToastProvider>
      <PoolsView />
    </ToastProvider>,
  );
}

describe("PoolsView", () => {
  it("loads pools and selects the first one", async () => {
    const fetchMock = vi.fn((url: string) => {
      if (url === "/api/pools") {
        return Promise.resolve(makeResponse(200, pools));
      }
      // pool GET (default selected first)
      return Promise.resolve(
        makeResponse(200, url.includes("/skills") ? [] : tree("default")),
      );
    });
    vi.stubGlobal("fetch", fetchMock);
    renderView();
    await waitFor(() => expect(screen.getByText("default")).toBeTruthy());
    expect(screen.getByText("research")).toBeTruthy();
    // editor header shows the selected pool (after its GET resolves)
    await waitFor(() => expect(screen.getByText(/Pool: default/)).toBeTruthy());
  });

  it("delete pool opens a custom ConfirmDialog (not window.confirm) and 409 surfaces a toast", async () => {
    const fetchMock = vi.fn((url: string, init?: RequestInit) => {
      const method = init?.method ?? "GET";
      if (url === "/api/pools") return Promise.resolve(makeResponse(200, pools));
      if (url === "/api/pools/research" && method === "DELETE") {
        return Promise.resolve(makeResponse(409, { error: "in use" }));
      }
      return Promise.resolve(
        makeResponse(200, url.includes("/skills") ? [] : tree("default")),
      );
    });
    vi.stubGlobal("fetch", fetchMock);
    renderView();
    await waitFor(() => expect(screen.getByText("research")).toBeTruthy());
    // hover-only delete button — find by aria-label
    fireEvent.click(screen.getByRole("button", { name: "Delete research" }));
    // custom confirm dialog appears (no window.confirm)
    expect(screen.getByRole("dialog")).toBeTruthy();
    expect(screen.getByText(/Delete pool "research"\?/)).toBeTruthy();
    // confirm
    fireEvent.click(screen.getByRole("button", { name: "Delete" }));
    await waitFor(() =>
      expect(screen.getByText(/Cannot delete "research"/)).toBeTruthy(),
    );
  });

  it("switching pool while dirty shows a custom confirm dialog", async () => {
    const fetchMock = vi.fn((url: string, init?: RequestInit) => {
      const method = init?.method ?? "GET";
      if (url === "/api/pools") return Promise.resolve(makeResponse(200, pools));
      if (url === "/api/pools/default" && method === "GET") {
        return Promise.resolve(
        makeResponse(200, url.includes("/skills") ? [] : tree("default")),
      );
      }
      if (url === "/api/pools/research" && method === "GET") {
        return Promise.resolve(makeResponse(200, tree("research")));
      }
      return Promise.resolve(makeResponse(200, url.includes("/skills") ? [] : {}));
    });
    vi.stubGlobal("fetch", fetchMock);
    renderView();
    await waitFor(() => expect(screen.getByText(/Pool: default/)).toBeTruthy());
    // make the editor dirty
    fireEvent.change(screen.getByDisplayValue("main"), {
      target: { value: "boss" },
    });
    // click the other pool → confirm dialog (no window.confirm)
    fireEvent.click(screen.getByText("research"));
    expect(screen.getByRole("dialog")).toBeTruthy();
    expect(screen.getByText("Discard unsaved changes?")).toBeTruthy();
    // confirming the dialog actually switches
    fireEvent.click(screen.getByRole("button", { name: "Discard" }));
    await waitFor(() => expect(screen.getByText(/Pool: research/)).toBeTruthy());
  });

  it("Add pool creates a new pool via POST /api/pools", async () => {
    const fetchMock = vi.fn((url: string, init?: RequestInit) => {
      const method = init?.method ?? "GET";
      if (url === "/api/pools" && method === "POST") {
        return Promise.resolve(makeResponse(200, tree("newpool")));
      }
      if (url === "/api/pools") return Promise.resolve(makeResponse(200, pools));
      return Promise.resolve(
        makeResponse(200, url.includes("/skills") ? [] : tree("default")),
      );
    });
    vi.stubGlobal("fetch", fetchMock);
    renderView();
    await waitFor(() => expect(screen.getByText("default")).toBeTruthy());
    fireEvent.click(screen.getByRole("button", { name: "Add pool" }));
    const input = await waitFor(() => screen.getByPlaceholderText("new-pool-name"));
    fireEvent.change(input, { target: { value: "newpool" } });
    fireEvent.keyDown(input, { key: "Enter" });
    await waitFor(() => {
      type Call = [unknown, RequestInit?];
      const calls = fetchMock.mock.calls as unknown as Call[];
      const posts = calls.filter((c) => c[1]?.method === "POST");
      expect(posts.length).toBeGreaterThanOrEqual(1);
    });
  });
});
