import { describe, it, expect, vi, afterEach, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { SettingsModal } from "./SettingsView";
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

const modelPayload = {
  domain: "model",
  label: "Models",
  flavor: "singleton" as const,
  restart_required: false,
  values: {
    default_provider: "DeepSeek",
    default_model: "m1",
    max_context_tokens: 200000,
    providers: [
      {
        key: "deepseek",
        name: "DeepSeek",
        base_url: "https://api.deepseek.com",
        interface_format: "openai_compatible",
        api_key: { has_value: true, hint: "••••" },
        models: [
          {
            name: "m1",
            model: "m1",
            capabilities: ["text"],
            temperature: 0.7,
            max_output_tokens: 50000,
            reasoning_effort: "none",
          },
        ],
      },
    ],
  },
};

// URL state persists across tests in happy-dom; reset ?tab= so each test
// starts on the IM view (the default) regardless of the previous test's nav.
beforeEach(() => {
  window.history.replaceState(null, "", "/");
});

afterEach(() => vi.unstubAllGlobals());

describe("SettingsModal", () => {
  it("renders null when open is false", () => {
    vi.stubGlobal("fetch", routeFetch());
    const { container } = render(
      <ToastProvider>
        <SettingsModal open={false} onClose={() => {}} />
      </ToastProvider>,
    );
    expect(container.firstChild).toBeNull();
  });

  it("renders settings navigation beside content in the modal", async () => {
    window.history.replaceState(null, "", "/?tab=scope");
    vi.stubGlobal("fetch", routeFetch());

    render(
      <ToastProvider>
        <SettingsModal open={true} onClose={() => {}} />
      </ToastProvider>,
    );

    const shell = await waitFor(() => screen.getByTestId("settings-shell"));
    const navigation = screen.getByRole("complementary", {
      name: "Settings navigation",
    });
    expect(shell.className).toContain("flex-row");
    expect(navigation.className).toContain("w-52");
    // The scope declaration editor is the pool-tree surface (ticket 11).
    expect(screen.getAllByText("Scope").length).toBeGreaterThan(0);
  });

  it("loads config and renders a field", async () => {
    vi.stubGlobal("fetch", vi.fn(() => Promise.resolve(makeResponse(200, JSON.stringify(imPayload)))));
    render(
      <ToastProvider>
        <SettingsModal open={true} onClose={() => {}} />
      </ToastProvider>,
    );
    await waitFor(() => expect(screen.getByText("App ID")).toBeTruthy());
  });

  it("Save disabled until dirty; Cancel reverts without PUT", async () => {
    const fetchMock = vi.fn(() => Promise.resolve(makeResponse(200, JSON.stringify(imPayload))));
    vi.stubGlobal("fetch", fetchMock);
    render(
      <ToastProvider>
        <SettingsModal open={true} onClose={() => {}} />
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
        <SettingsModal open={true} onClose={() => {}} />
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

  it("shows the unsaved-changes ember dot only while dirty", async () => {
    const fetchMock = vi.fn(() => Promise.resolve(makeResponse(200, JSON.stringify(imPayload))));
    vi.stubGlobal("fetch", fetchMock);
    render(
      <ToastProvider>
        <SettingsModal open={true} onClose={() => {}} />
      </ToastProvider>,
    );
    await waitFor(() => expect(screen.getByDisplayValue("A")).toBeTruthy());
    // Clean state — no unsaved-changes indicator.
    expect(screen.queryByRole("status")).toBeNull();
    // Edit → dirty → ember dot appears.
    fireEvent.change(screen.getByDisplayValue("A"), { target: { value: "B" } });
    expect(screen.getByRole("status")).toBeTruthy();
    expect(screen.getByRole("status").getAttribute("aria-label")).toBe("Unsaved changes");
    // Cancel reverts → dot disappears.
    fireEvent.click(screen.getByText("Cancel"));
    expect(screen.queryByRole("status")).toBeNull();
  });


  // The IM view must load first so the persisted-domain gate is satisfied; then
  // we click into MCP / Skills and assert each child's distinctive copy
  // appears. Guards against the regression where a placeholder was rendered
  // instead of the real view.
  function routeFetch(): ReturnType<typeof vi.fn> {
    return vi.fn((input: RequestInfo | URL) => {
      const url = String(input);
      const body =
        url.endsWith("/api/pools")
          ? []
          : url.endsWith("/api/scope/declaration")
            ? { yaml: "workspace:\n  name: bot\n  pools: {}\n" }
            : url.endsWith("/api/scope/topology")
              ? { kind: "workspace", workspace: "bot", pools: [] }
              : url.endsWith("/api/scope/bill")
                ? { agents: [] }
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

  it("MCP sidebar route renders GlobalMcpView", async () => {
    vi.stubGlobal("fetch", routeFetch());
    render(
      <ToastProvider>
        <SettingsModal open={true} onClose={() => {}} />
      </ToastProvider>,
    );
    await waitFor(() => expect(screen.getByText("App ID")).toBeTruthy());
    fireEvent.click(screen.getByText("MCP"));
    await waitFor(() =>
      expect(
        screen.getByText("MCP servers available to every pool's agents."),
      ).toBeTruthy(),
    );
  });

  it("Skills sidebar route renders GlobalSkillsView", async () => {
    vi.stubGlobal("fetch", routeFetch());
    render(
      <ToastProvider>
        <SettingsModal open={true} onClose={() => {}} />
      </ToastProvider>,
    );
    await waitFor(() => expect(screen.getByText("App ID")).toBeTruthy());
    fireEvent.click(screen.getByText("Skills"));
    await waitFor(() =>
      expect(
        screen.getByText("Skills available to every pool's agents."),
      ).toBeTruthy(),
    );
  });

  it("Skills: clicking a skill row expands its detail pane", async () => {
    let skillsCalled = false;
    vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL) => {
      const url = String(input);
      const body =
        url.endsWith("/api/skills")
          ? (skillsCalled = true, [{ name: "weather", source: "global", origin: "repo", description: "Get weather forecasts." }])
          : url.endsWith("/api/config/im")
            ? imPayload
            : {};
      return Promise.resolve(makeResponse(200, JSON.stringify(body)));
    }));
    render(
      <ToastProvider>
        <SettingsModal open={true} onClose={() => {}} />
      </ToastProvider>,
    );
    // Wait for IM to load first, then navigate to Skills.
    await waitFor(() => expect(screen.getByText("App ID")).toBeTruthy());
    fireEvent.click(screen.getByText("Skills"));
    await waitFor(() => {
      expect(skillsCalled).toBe(true);
    });
    // Wait for the weather row.
    await waitFor(() => expect(screen.getByText("weather")).toBeTruthy());
    fireEvent.click(screen.getAllByText("weather")[0]!);
    await waitFor(() =>
      expect(screen.getAllByText("Get weather forecasts.").length).toBeGreaterThan(0),
    );
    // Detail pane should also show delete button; source badge stays on the row.
    expect(screen.getByRole("button", { name: "Delete skill weather" })).toBeTruthy();
  });

  describe("Models save validation", () => {
    it("allows save when default model is cleared — PUT issued (Ticket #4 relaxation)", async () => {
      let putCalled = false;
      const fetchMock = vi.fn((_input: RequestInfo | URL, init?: RequestInit) => {
        const method = init?.method ?? "GET";
        if (method === "PUT") {
          putCalled = true;
          return Promise.resolve(makeResponse(200, JSON.stringify(modelPayload)));
        }
        return Promise.resolve(makeResponse(200, JSON.stringify(modelPayload)));
      });
      vi.stubGlobal("fetch", fetchMock);

      window.history.replaceState(null, "", "/?tab=model");
      render(
        <ToastProvider>
          <SettingsModal open={true} onClose={() => {}} />
        </ToastProvider>,
      );

      await waitFor(() => expect(screen.getByDisplayValue("DeepSeek")).toBeTruthy());

      fireEvent.click(screen.getByRole("button", { name: "Remove model" }));
      fireEvent.click(screen.getByRole("button", { name: "Delete" }));

      fireEvent.click(screen.getByText("Save"));

      // Ticket #4: empty default is now valid — PUT must go through.
      await waitFor(() => {
        expect(putCalled).toBe(true);
      });

      expect(
        screen.queryByText(/Select a default model before saving/),
      ).toBeNull();
    });

    it("shows a friendly error from a 400 ApiError JSON body with fields", async () => {
      const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        const method = init?.method ?? "GET";
        if (url.endsWith("/api/config/model") && method === "GET") {
          return Promise.resolve(makeResponse(200, JSON.stringify(modelPayload)));
        }
        if (url.endsWith("/api/config/model") && method === "PUT") {
          return Promise.resolve(
            new Response(
              JSON.stringify({
                error: "validation",
                fields: { default_model: ["is required"] },
              }),
              { status: 400 },
            ),
          );
        }
        return Promise.resolve(makeResponse(200, JSON.stringify(modelPayload)));
      });
      vi.stubGlobal("fetch", fetchMock);

      window.history.replaceState(null, "", "/?tab=model");
      render(
        <ToastProvider>
          <SettingsModal open={true} onClose={() => {}} />
        </ToastProvider>,
      );

      await waitFor(() => expect(screen.getByDisplayValue("DeepSeek")).toBeTruthy());

      fireEvent.change(screen.getByDisplayValue("200000"), {
        target: { value: "128000" },
      });

      fireEvent.click(screen.getByText("Save"));

      await waitFor(() => {
        expect(screen.getByText(/Save failed:/)).toBeTruthy();
      });
      expect(screen.getByText(/default_model: is required/)).toBeTruthy();
    });

    it("shows a friendly error from a 400 ApiError JSON body with error string", async () => {
      const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        const method = init?.method ?? "GET";
        if (url.endsWith("/api/config/model") && method === "GET") {
          return Promise.resolve(makeResponse(200, JSON.stringify(modelPayload)));
        }
        if (url.endsWith("/api/config/model") && method === "PUT") {
          return Promise.resolve(
            new Response(
              JSON.stringify({ error: "Something went wrong" }),
              { status: 400 },
            ),
          );
        }
        return Promise.resolve(makeResponse(200, JSON.stringify(modelPayload)));
      });
      vi.stubGlobal("fetch", fetchMock);

      window.history.replaceState(null, "", "/?tab=model");
      render(
        <ToastProvider>
          <SettingsModal open={true} onClose={() => {}} />
        </ToastProvider>,
      );

      await waitFor(() => expect(screen.getByDisplayValue("DeepSeek")).toBeTruthy());

      fireEvent.change(screen.getByDisplayValue("200000"), {
        target: { value: "128000" },
      });

      fireEvent.click(screen.getByText("Save"));

      await waitFor(() => {
        expect(screen.getByText(/Save failed: Something went wrong/)).toBeTruthy();
      });
    });
  });
});
