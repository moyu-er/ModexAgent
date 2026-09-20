import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ToastProvider } from "../../ToastContext";
import { PoolsConfigView } from "./PoolsConfigView";

function response(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

const MODEL = {
  workspace: {
    name: "bot",
    pools: { main: { agents: { main: { description: "root" } } } },
  },
};

const OPTIONS = {
  toolsets: ["full"],
  context_modes: ["fresh", "fork"],
  execution_strategies: ["react", "external"],
  provider_kinds: [],
  capabilities: [],
  capability_bundles: {},
  hooks: [],
  default_hooks: [],
  interceptors: [],
  commands: [],
  mcp_servers: [],
  position_defaults: {
    root: { toolset: "full", registration: "eager" },
    sub: { toolset: "read_write", registration: "lazy" },
  },
};

const BILL = {
  agents: [
    {
      pool: "main",
      agent: "main",
      root: true,
      fields: [],
      tools: [],
      tool_groups: [],
      hooks: [],
      capabilities: [],
    },
  ],
};

afterEach(() => vi.unstubAllGlobals());

describe("PoolsConfigView save", () => {
  it("does not arm restart UI when the save response says restart is not required", async () => {
    let savedModel = structuredClone(MODEL);
    const fetchMock = vi.fn((url: string, init?: RequestInit) => {
      if (url === "/api/scope/model" && init?.method === "PUT") {
        savedModel = JSON.parse(String(init.body)).model;
        return Promise.resolve(response({ saved: true, restart_required: false }));
      }
      if (url === "/api/scope/model") return Promise.resolve(response({ model: savedModel }));
      if (url === "/api/scope/options") return Promise.resolve(response(OPTIONS));
      if (url === "/api/scope/bill") return Promise.resolve(response(BILL));
      if (url === "/api/scope/preview") return Promise.resolve(response(BILL));
      if (url === "/api/prompts") return Promise.resolve(response([]));
      return Promise.resolve(response({}));
    });
    vi.stubGlobal("fetch", fetchMock);
    render(
      <ToastProvider>
        <PoolsConfigView />
      </ToastProvider>,
    );
    await waitFor(() => expect(screen.getByTestId("pools-view")).toBeTruthy());
    // The ordinary face only — no recursive technical tree.
    expect(screen.queryByRole("tree")).toBeNull();
    expect(screen.queryByTestId("pools-structure-toggle")).toBeNull();
    fireEvent.change(screen.getByLabelText("Description"), {
      target: { value: "Ordinary edit" },
    });
    fireEvent.click(screen.getByTestId("pools-save"));
    await waitFor(() =>
      expect(
        fetchMock.mock.calls.some(
          ([url, init]) => url === "/api/scope/model" && init?.method === "PUT",
        ),
      ).toBe(true),
    );
    expect(screen.queryByText("Saved. Restart to apply.")).toBeNull();
  });

  it("blocks deleting the preferred default assistant with a change-default-first notice (PA-15)", async () => {
    const fetchMock = vi.fn((url: string, init?: RequestInit) => {
      if (url === "/api/config/personal_assistant") {
        return Promise.resolve(
          response({
            values: { default_workspace: null, default_pool: "main" },
          }),
        );
      }
      if (url === "/api/scope/model") return Promise.resolve(response({ model: savedModel }));
      if (url === "/api/scope/options") return Promise.resolve(response(OPTIONS));
      if (url === "/api/scope/bill") return Promise.resolve(response(BILL));
      if (url === "/api/scope/preview") return Promise.resolve(response(BILL));
      if (url === "/api/prompts") return Promise.resolve(response([]));
      void init;
      return Promise.resolve(response({}));
    });
    let savedModel = structuredClone(MODEL);
    vi.stubGlobal("fetch", fetchMock);
    render(
      <ToastProvider>
        <PoolsConfigView />
      </ToastProvider>,
    );
    await waitFor(() => expect(screen.getByTestId("pools-view")).toBeTruthy());
    // The tree renders a "Delete main" control on both the pool row and its
    // root agent row — either way the target is the whole pool.
    const deleteBtns = await waitFor(() => {
      const btns = screen.getAllByRole("button", { name: "Delete main" });
      expect(btns.length).toBeGreaterThan(0);
      return btns;
    });
    fireEvent.click(deleteBtns[0]!);
    await waitFor(() => {
      expect(screen.getByText("Change the default assistant first")).toBeTruthy();
      expect(
        screen.getByText(
          '"main" is the default assistant. Choose another default and save before deleting it.',
        ),
      ).toBeTruthy();
    });
    // The normal deletion confirm never appears.
    expect(screen.queryByText(/The pool, its agents, and its peer links/)).toBeNull();
  });
});
