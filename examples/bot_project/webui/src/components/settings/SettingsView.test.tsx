import { describe, it, expect, vi, afterEach, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { SettingsView } from "./SettingsView";
import { ToastProvider } from "../ToastContext";

function makeResponse(status: number, body: string): Response {
  return new Response(body, { status });
}

const imPayload = {
  domain: "im",
  label: "IM Adapters",
  flavor: "registry" as const,
  restart_required: false,
  sections: {
    qq: {
      label: "QQ",
      values: {
        enabled: true,
        app_id: "A",
        secret: { has_value: true, hint: "••••" },
        sandbox: false,
        allow_from: ["*"],
      },
      fields: [
        { name: "app_id", label: "App ID", type: "string", required: false },
        { name: "secret", label: "Secret", type: "secret", required: false },
      ],
    },
  },
};

// URL state persists across tests in happy-dom; reset ?tab= so each test
// starts on the IM view (the default) regardless of the previous test's nav.
beforeEach(() => {
  window.history.replaceState(null, "", "/");
});

afterEach(() => vi.unstubAllGlobals());

describe("SettingsView", () => {
  it("loads config and renders a field", async () => {
    vi.stubGlobal("fetch", vi.fn(() => Promise.resolve(makeResponse(200, JSON.stringify(imPayload)))));
    render(
      <ToastProvider>
        <SettingsView onExit={() => {}} />
      </ToastProvider>,
    );
    await waitFor(() => expect(screen.getByText("App ID")).toBeTruthy());
  });

  it("Save disabled until dirty; Cancel reverts without PUT", async () => {
    const fetchMock = vi.fn(() => Promise.resolve(makeResponse(200, JSON.stringify(imPayload))));
    vi.stubGlobal("fetch", fetchMock);
    render(
      <ToastProvider>
        <SettingsView onExit={() => {}} />
      </ToastProvider>,
    );
    await waitFor(() => expect(screen.getByDisplayValue("A")).toBeTruthy());
    const saveBtn = screen.getByText("Save") as HTMLButtonElement;
    const cancelBtn = screen.getByText("Cancel") as HTMLButtonElement;
    expect(saveBtn.disabled).toBe(true);
    expect(cancelBtn.disabled).toBe(true);
    // edit
    fireEvent.change(screen.getByDisplayValue("A"), { target: { value: "B" } });
    expect(saveBtn.disabled).toBe(false);
    expect(cancelBtn.disabled).toBe(false);
    // cancel reverts
    fireEvent.click(screen.getByText("Cancel"));
    expect((screen.getByDisplayValue("A") as HTMLInputElement).value).toBe("A");
    // only the initial GET was issued — no PUT
    type Call = [unknown, RequestInit?];
    const calls = fetchMock.mock.calls as unknown as Call[];
    const puts = calls.filter((c) => c[1]?.method === "PUT");
    expect(puts).toHaveLength(0);
  });

  it("Save issues PUT and surfaces the restart toast when restart_required", async () => {
    const putResponse = { ...imPayload, restart_required: true };
    vi.stubGlobal("fetch", vi.fn(() => Promise.resolve(makeResponse(200, JSON.stringify(putResponse)))));
    render(
      <ToastProvider>
        <SettingsView onExit={() => {}} />
      </ToastProvider>,
    );
    await waitFor(() => expect(screen.getByDisplayValue("A")).toBeTruthy());
    fireEvent.change(screen.getByDisplayValue("A"), { target: { value: "B" } });
    fireEvent.click(screen.getByText("Save"));
    // Restart messaging is now uniform: a toast, not an inline banner.
    await waitFor(() =>
      expect(screen.getByText("Saved. Restart to apply.")).toBeTruthy(),
    );
    type Call = [unknown, RequestInit?];
    const calls = (fetch as unknown as { mock: { calls: Call[] } }).mock.calls;
    const puts = calls.filter((c) => c[1]?.method === "PUT");
    expect(puts.length).toBeGreaterThanOrEqual(1);
  });

  // Render-smoke: each non-persisted sidebar route mounts the right child view.
  // The IM view must load first so the persisted-domain gate is satisfied; then
  // we click into Pools / Global MCP / Global Skills and assert each child's
  // distinctive copy appears. Guards against the regression where a placeholder
  // was rendered instead of the real view.
  function routeFetch(): ReturnType<typeof vi.fn> {
    return vi.fn((input: RequestInfo | URL) => {
      const url = String(input);
      const body =
        url.endsWith("/api/pools")
          ? []
          : url.endsWith("/api/mcp")
            ? {}
            : url.endsWith("/api/skills")
              ? []
              : url.endsWith("/api/config/im")
                ? imPayload
                : {};
      return Promise.resolve(makeResponse(200, JSON.stringify(body)));
    });
  }

  it("Pools sidebar route renders PoolsView", async () => {
    vi.stubGlobal("fetch", routeFetch());
    render(
      <ToastProvider>
        <SettingsView onExit={() => {}} />
      </ToastProvider>,
    );
    // IM loads by default; wait for it to settle before switching.
    await waitFor(() => expect(screen.getByText("App ID")).toBeTruthy());
    fireEvent.click(screen.getByText("Pools"));
    // PoolsView renders an "Add pool" icon button — absent from every other route.
    await waitFor(() => expect(screen.getByLabelText("Add pool")).toBeTruthy());
  });

  it("Global MCP sidebar route renders GlobalMcpView", async () => {
    vi.stubGlobal("fetch", routeFetch());
    render(
      <ToastProvider>
        <SettingsView onExit={() => {}} />
      </ToastProvider>,
    );
    await waitFor(() => expect(screen.getByText("App ID")).toBeTruthy());
    fireEvent.click(screen.getByText("Global MCP"));
    await waitFor(() =>
      expect(
        screen.getByText("Global MCP servers available to every pool's agents."),
      ).toBeTruthy(),
    );
  });

  it("Global Skills sidebar route renders GlobalSkillsView", async () => {
    vi.stubGlobal("fetch", routeFetch());
    render(
      <ToastProvider>
        <SettingsView onExit={() => {}} />
      </ToastProvider>,
    );
    await waitFor(() => expect(screen.getByText("App ID")).toBeTruthy());
    fireEvent.click(screen.getByText("Global Skills"));
    await waitFor(() =>
      expect(
        screen.getByText("Global skills available to every pool's agents."),
      ).toBeTruthy(),
    );
  });

  it("Global Skills: clicking a skill row expands its detail pane", async () => {
    let skillsCalled = false;
    vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL) => {
      const url = String(input);
      const body =
        url.endsWith("/api/skills")
          ? (skillsCalled = true, [{ name: "weather", source: "global", description: "Get weather forecasts." }])
          : url.endsWith("/api/config/im")
            ? imPayload
            : {};
      return Promise.resolve(makeResponse(200, JSON.stringify(body)));
    }));
    render(
      <ToastProvider>
        <SettingsView onExit={() => {}} />
      </ToastProvider>,
    );
    // Wait for IM to load first, then navigate to Global Skills.
    await waitFor(() => expect(screen.getByText("App ID")).toBeTruthy());
    fireEvent.click(screen.getByText("Global Skills"));
    await waitFor(() => {
      expect(skillsCalled).toBe(true);
    });
    // Wait for the weather row.
    await waitFor(() => expect(screen.getByText("weather")).toBeTruthy());
    // Click the weather skill row — this should expand the detail pane.
    fireEvent.click(screen.getByText("weather"));
    await waitFor(() =>
      expect(screen.getByText("Get weather forecasts.")).toBeTruthy(),
    );
    // Detail pane should also show delete button; source badge stays on the row.
    expect(screen.getByRole("button", { name: "Delete skill weather" })).toBeTruthy();
  });
});
