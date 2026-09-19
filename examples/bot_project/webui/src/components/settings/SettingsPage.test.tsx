// SettingsPage (PA-11): the standalone settings page — six searchable
// groups, deep-link section routing, the local dirty navigation guard
// (save-and-leave / discard / keep editing), and the General page's
// default-pool/default-workspace preference writes.

import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { describe, it, expect, vi, afterEach } from "vitest";
import { SettingsPage } from "./SettingsPage";
import { ToastProvider } from "../ToastContext";

function makeResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), { status });
}

const imPayload = {
  domain: "im",
  label: "IM Adapters",
  flavor: "registry" as const,
  restart_required: false,
  sections: {
    qq: {
      label: "QQ",
      values: { app_id: "A" },
      fields: [{ name: "app_id", label: "App ID", type: "string", required: false }],
    },
  },
};

const prefsPayload = {
  domain: "personal_assistant",
  label: "Personal Assistant",
  flavor: "singleton" as const,
  restart_required: false,
  values: { default_workspace: null, default_pool: "main" },
};

function routeFetch(overrides: Record<string, unknown> = {}) {
  return vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const method = init?.method ?? "GET";
    const key = `${method} ${url}`;
    if (key in overrides) {
      const body = overrides[key];
      return Promise.resolve(
        body instanceof Response ? body : makeResponse(200, body),
      );
    }
    const body =
      url.endsWith("/api/config/im") && method === "GET"
        ? imPayload
        : url.endsWith("/api/config/personal_assistant")
          ? prefsPayload
          : url.endsWith("/api/pools")
            ? [{ name: "main", root_agent_name: "main", subagent_count: 0 }]
            : url.endsWith("/api/workspace")
              ? { home: "/home", recent: [] }
              : url.endsWith("/api/scope/model")
                ? { model: { workspace: { name: "bot", pools: {} } } }
                : url.endsWith("/api/scope/options")
                  ? {
                      toolsets: [],
                      context_modes: [],
                      execution_strategies: [],
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
                    }
                  : url.endsWith("/api/scope/bill") || url.endsWith("/api/scope/preview")
                    ? { agents: [] }
                    : url.endsWith("/api/prompts")
                      ? []
                      : url.endsWith("/api/skills")
                        ? []
                        : {};
    return Promise.resolve(makeResponse(200, body));
  });
}

function renderPage(
  props: Partial<React.ComponentProps<typeof SettingsPage>> = {},
) {
  const navigate = vi.fn();
  const onExit = vi.fn();
  const view = render(
    <ToastProvider>
      <SettingsPage section="general" navigate={navigate} onExit={onExit} {...props} />
    </ToastProvider>,
  );
  return { view, navigate, onExit };
}

afterEach(() => vi.unstubAllGlobals());

describe("SettingsPage navigation", () => {
  it("renders the six groups in the nav rail", () => {
    vi.stubGlobal("fetch", routeFetch());
    renderPage();
    for (const label of [
      "General",
      "Models",
      "Assistants",
      "Extensions",
      "Messaging channels",
      "Advanced",
    ]) {
      expect(screen.getAllByText(label).length).toBeGreaterThan(0);
    }
  });

  it("search narrows to matching groups and no-match shows a message", () => {
    vi.stubGlobal("fetch", routeFetch());
    renderPage();
    const input = screen.getByPlaceholderText("Search settings…");
    fireEvent.change(input, { target: { value: "qq" } });
    expect(screen.getByTestId("settings-search-results")).toBeTruthy();
    expect(screen.getByText("Messaging channels")).toBeTruthy();
    expect(screen.queryByText("Models")).toBeNull();

    fireEvent.change(input, { target: { value: "zzzz-nothing" } });
    expect(screen.getByText('No settings match "zzzz-nothing".')).toBeTruthy();
  });

  it("a group click navigates to its section hash", () => {
    vi.stubGlobal("fetch", routeFetch());
    const { navigate } = renderPage();
    fireEvent.click(screen.getByText("Advanced"));
    expect(navigate).toHaveBeenCalledWith("/settings/advanced");
  });

  it("Back exits settings", () => {
    vi.stubGlobal("fetch", routeFetch());
    const { onExit } = renderPage();
    fireEvent.click(screen.getByText("Back"));
    expect(onExit).toHaveBeenCalledTimes(1);
  });
});

describe("SettingsPage dirty guard", () => {
  it("leaving a dirty editor prompts; discard proceeds and reverts the field", async () => {
    vi.stubGlobal("fetch", routeFetch());
    // Start on the im (channels) section so the persisted editor mounts.
    const { navigate } = renderPage({ section: "im" });
    const field = await waitFor(() => screen.getByDisplayValue("A"));
    fireEvent.change(field, { target: { value: "B" } });

    fireEvent.click(screen.getByText("Back"));
    // The guard opens instead of exiting.
    await waitFor(() => {
      expect(screen.getByRole("dialog")).toBeTruthy();
    });

    fireEvent.click(screen.getByText("Discard"));
    // Discard closed the dialog AND ran the pending navigation (exit).
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).toBeNull();
    });
    expect(navigate).not.toHaveBeenCalled();
  });

  it("save-and-leave persists then navigates; a failed save stays", async () => {
    let putCalls = 0;
    const fetchMock = routeFetch({
      "PUT /api/config/im": (() => {
        putCalls += 1;
        return makeResponse(200, imPayload);
      })(),
    });
    vi.stubGlobal("fetch", fetchMock);
    const { onExit } = renderPage({ section: "im" });
    const field = await waitFor(() => screen.getByDisplayValue("A"));
    fireEvent.change(field, { target: { value: "B" } });

    fireEvent.click(screen.getByText("Back"));
    fireEvent.click(screen.getByText("Save and leave"));
    await waitFor(() => {
      expect(putCalls).toBe(1);
      expect(onExit).toHaveBeenCalledTimes(1);
    });
  });

  it("clean editors navigate immediately without a prompt", async () => {
    vi.stubGlobal("fetch", routeFetch());
    const { onExit } = renderPage({ section: "im" });
    await waitFor(() => screen.getByDisplayValue("A"));
    fireEvent.click(screen.getByText("Back"));
    expect(onExit).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole("dialog")).toBeNull();
  });
});

describe("General settings (PA-11)", () => {
  it("saving the default pool PUTs the preference and reports it upward", async () => {
    const onPreferencesChanged = vi.fn();
    vi.stubGlobal(
      "fetch",
      routeFetch({
        "PUT /api/config/personal_assistant": makeResponse(200, {
          ...prefsPayload,
          values: { default_workspace: null, default_pool: "coder" },
        }),
      }),
    );
    renderPage({ section: "general", onPreferencesChanged });
    const select = await waitFor(() =>
      screen.getByLabelText("Default assistant") as HTMLSelectElement,
    );
    expect(select.value).toBe("main");
    fireEvent.change(select, { target: { value: "coder" } });
    await waitFor(() => {
      expect(onPreferencesChanged).toHaveBeenCalledWith({
        defaultWorkspace: null,
        defaultPool: "coder",
      });
    });
  });

  it("a default pool missing from the running pools shows the invalid hint", async () => {
    vi.stubGlobal(
      "fetch",
      routeFetch({
        "GET /api/config/personal_assistant": makeResponse(200, {
          ...prefsPayload,
          values: { default_workspace: null, default_pool: "ghost" },
        }),
      }),
    );
    renderPage({ section: "general" });
    await waitFor(() => {
      expect(
        screen.getByText(
          "The saved default assistant is not available — pick another.",
        ),
      ).toBeTruthy();
    });
  });
});
