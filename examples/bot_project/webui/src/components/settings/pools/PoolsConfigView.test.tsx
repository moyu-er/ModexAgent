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

    fireEvent.change(screen.getByLabelText("Data directory name"), {
      target: { value: ".custom" },
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
});
