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
  peers: [],
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

const externalTree = (name: string) => ({
  ...tree(name),
  main: {
    ...tree(name).main,
    execution_strategy: "external",
    provider_kind: "opencode",
  },
});

afterEach(() => vi.unstubAllGlobals());

async function renderView(): Promise<void> {
  render(
    <ToastProvider>
      <PoolsView onNavigateToPrompts={() => {}} />
    </ToastProvider>,
  );
  await waitFor(() =>
    expect(screen.queryByText("Loading…")).toBeNull(),
  );
}

describe("PoolsView", () => {
  it("stacks the pool list above the external editor on narrow screens", async () => {
    const fetchMock = vi.fn((url: string) => {
      if (url === "/api/pools") {
        return Promise.resolve(makeResponse(200, pools));
      }
      return Promise.resolve(
        makeResponse(
          200,
          url.includes("/skills") || url === "/api/prompts" ? [] : externalTree("default"),
        ),
      );
    });
    vi.stubGlobal("fetch", fetchMock);

    await renderView();

    const layout = await waitFor(() => screen.getByTestId("pools-layout"));
    const poolList = screen.getByRole("complementary", { name: "Pool list" });
    const editor = screen.getByRole("region", { name: "Selected pool editor" });
    expect(layout.className).toContain("flex-col");
    expect(layout.className).toContain("lg:flex-row");
    expect(poolList.className).toContain("max-h-48");
    expect(poolList.className).toContain("lg:w-64");
    expect(editor.className).toContain("min-w-0");
    expect(screen.getByLabelText("Implementation")).toBeTruthy();
    expect(screen.getByLabelText("Provider")).toBeTruthy();
    expect(screen.getByRole("group", { name: "Form actions" })).toBeTruthy();
  });

  it("loads pools and selects the first one", async () => {
    const fetchMock = vi.fn((url: string) => {
      if (url === "/api/pools") {
        return Promise.resolve(makeResponse(200, pools));
      }
      // pool GET (default selected first)
      return Promise.resolve(
        makeResponse(200, url.includes("/skills") || url === "/api/prompts" ? [] : tree("default")),
      );
    });
    vi.stubGlobal("fetch", fetchMock);
    await renderView();
    await waitFor(() => expect(screen.getByText("default")).toBeTruthy());
    expect(screen.getByText("research")).toBeTruthy();
    // editor header shows the selected pool (after its GET resolves)
    await waitFor(() => expect(screen.getByText(/Pool: default/)).toBeTruthy());
  });

  it("delete pool opens a custom ConfirmDialog (not window.confirm) and errors surface a toast", async () => {
    const fetchMock = vi.fn((url: string, init?: RequestInit) => {
      const method = init?.method ?? "GET";
      if (url === "/api/pools") return Promise.resolve(makeResponse(200, pools));
      if (url === "/api/pools/research" && method === "DELETE") {
        return Promise.resolve(makeResponse(500, { error: "in use" }));
      }
      return Promise.resolve(
        makeResponse(200, url.includes("/skills") || url === "/api/prompts" ? [] : tree("default")),
      );
    });
    vi.stubGlobal("fetch", fetchMock);
    await renderView();
    await waitFor(() => expect(screen.getByText("research")).toBeTruthy());
    fireEvent.click(screen.getByRole("button", { name: "Delete research" }));
    expect(screen.getByRole("dialog")).toBeTruthy();
    expect(screen.getByText(/Delete pool "research"\?/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Delete" }));
    await waitFor(() =>
      expect(screen.getByText(/Delete failed:/)).toBeTruthy(),
    );
  });

  it("switching pool while dirty shows a custom confirm dialog", async () => {
    const fetchMock = vi.fn((url: string, init?: RequestInit) => {
      const method = init?.method ?? "GET";
      if (url === "/api/pools") return Promise.resolve(makeResponse(200, pools));
      if (url === "/api/pools/default" && method === "GET") {
        return Promise.resolve(
          makeResponse(200, tree("default")),
        );
      }
      if (url === "/api/pools/research" && method === "GET") {
        return Promise.resolve(makeResponse(200, tree("research")));
      }
      return Promise.resolve(makeResponse(200, url.includes("/skills") || url === "/api/prompts" ? [] : {}));
    });
    vi.stubGlobal("fetch", fetchMock);
    await renderView();
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
        makeResponse(200, url.includes("/skills") || url === "/api/prompts" ? [] : tree("default")),
      );
    });
    vi.stubGlobal("fetch", fetchMock);
    await renderView();
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

  it("search filter narrows the list (case-insensitive substring)", async () => {
    const manyPools = [
      { name: "default", main_agent_name: "main", subagent_count: 0 },
      { name: "deep-research", main_agent_name: "main", subagent_count: 1 },
      { name: "ops", main_agent_name: "main", subagent_count: 0 },
      { name: "DEEP-ops", main_agent_name: "main", subagent_count: 0 },
    ];
    const fetchMock = vi.fn((url: string) => {
      if (url === "/api/pools") {
        return Promise.resolve(makeResponse(200, manyPools));
      }
      return Promise.resolve(
        makeResponse(200, url.includes("/skills") || url === "/api/prompts" ? [] : tree("default")),
      );
    });
    vi.stubGlobal("fetch", fetchMock);
    await renderView();
    await waitFor(() => expect(screen.getByText("default")).toBeTruthy());
    const filter = screen.getByLabelText("Filter pools");
    fireEvent.change(filter, { target: { value: "deep" } });
    // non-matching pools are hidden, matching pools still visible
    expect(screen.queryByText("default")).toBeNull();
    expect(screen.queryByText("ops")).toBeNull();
    expect(screen.getByText("deep-research")).toBeTruthy();
    expect(screen.getByText("DEEP-ops")).toBeTruthy();
  });

  it("pressing Enter inside the search bar opens the new-pool input", async () => {
    const fetchMock = vi.fn((url: string) => {
      if (url === "/api/pools") {
        return Promise.resolve(makeResponse(200, pools));
      }
      return Promise.resolve(
        makeResponse(200, url.includes("/skills") || url === "/api/prompts" ? [] : tree("default")),
      );
    });
    vi.stubGlobal("fetch", fetchMock);
    await renderView();
    await waitFor(() => expect(screen.getByText("default")).toBeTruthy());
    const filter = screen.getByLabelText("Filter pools");
    fireEvent.keyDown(filter, { key: "Enter" });
    expect(
      await waitFor(() => screen.getByPlaceholderText("new-pool-name")),
    ).toBeTruthy();
  });
});
