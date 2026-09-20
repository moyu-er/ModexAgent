import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ToastProvider } from "../../ToastContext";
import { SettingsPage } from "../SettingsPage";
import type { ScopeAgentBill, ScopeModelTree } from "../../../lib/scopeApi";

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

function setup(failScopeSave = false) {
  let tree: ScopeModelTree = { workspace: { name: "bot", pools: {
    main: { agents: { main: { description: "Original", prompt_name: "shared", max_steps: 71,
      agents: { helper: { description: "nested", agents: { deeper: { description: "deep" } } } },
    } } },
    other: { agents: { other: { description: "Other" } } },
  } } };
  let prefs = { default_workspace: "C:/keep", default_pool: "main" };
  let prompt = "Original instructions";
  const writes: string[] = [];
  const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
    const path = url.split("?")[0]!;
    const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status });
    if (path === "/api/config/personal_assistant") {
      if (init?.method === "PUT") { writes.push("preferences"); prefs = JSON.parse(String(init.body)); }
      return json({ values: prefs });
    }
    if (path === "/api/scope/model") {
      if (init?.method === "PUT") {
        writes.push("scope");
        if (failScopeSave) return json({ error: "disk unavailable" }, 500);
        tree = JSON.parse(String(init.body)).model;
        return json({ saved: true, restart_required: true });
      }
      return json({ model: tree });
    }
    if (path === "/api/scope/options") return json({ toolsets: ["full"], context_modes: ["fresh"],
      execution_strategies: ["react"], provider_kinds: [], capabilities: [], capability_bundles: {},
      hooks: ["session_title"], default_hooks: [], interceptors: [], commands: [], mcp_servers: [],
      position_defaults: { root: { toolset: "full", registration: "eager" }, sub: { toolset: "full", registration: "lazy" } },
    });
    if (path === "/api/scope/bill" || path === "/api/scope/preview") {
      const agents: ScopeAgentBill[] = ["main", "helper", "deeper"].map((agent) => ({
        pool: "main", agent, root: agent === "main", external: false,
        fields: [], tools: [], tool_groups: [], hooks: [],
        capabilities: [{ capability: "skills", state: "auto", registration_source: "framework", contributions: [] }],
        memory: { memory_preset: "session_only", archive_enabled: false, core_enabled: false },
        approval: { enabled: false, eligible: agent === "main" },
      }));
      return json({ agents });
    }
    if (path === "/api/prompts/shared") {
      if (init?.method === "PUT") { writes.push("prompt"); prompt = JSON.parse(String(init.body)).content; }
      return json({ name: "shared", content: prompt });
    }
    if (path === "/api/prompts") return json([{ name: "shared" }]);
    if (path === "/api/skills") return json([{ name: "research", source: "global" }]);
    if (path.endsWith("/skills")) return json([]);
    return json({});
  });
  vi.stubGlobal("fetch", fetchMock);
  const onPreferencesChanged = vi.fn();
  render(<ToastProvider><SettingsPage section="assistants" navigate={vi.fn()} onExit={vi.fn()} onPreferencesChanged={onPreferencesChanged} /></ToastProvider>);
  return { tree: () => tree, prefs: () => prefs, prompt: () => prompt, writes, onPreferencesChanged };
}

describe("assistant settings journeys", () => {
  it("preserves unsaved instructions when their section is collapsed", async () => {
    setup();
    await screen.findByDisplayValue("Original instructions");
    fireEvent.change(screen.getByDisplayValue("Original instructions"), { target: { value: "Keep local instructions" } });
    fireEvent.click(screen.getByRole("button", { name: "Overview" }));
    fireEvent.click(screen.getByRole("button", { name: "Overview" }));
    expect(screen.getByDisplayValue("Keep local instructions")).toBeTruthy();
    expect(screen.queryByRole("spinbutton", { name: "Max steps" })).toBeNull();
  });
  it("exposes no technical tree toggle — the normal assistants view is the only face", async () => {
    setup();
    await screen.findByDisplayValue("Original");
    expect(screen.queryByTestId("pools-structure-toggle")).toBeNull();
    expect(screen.queryByRole("tree")).toBeNull();
    expect(screen.queryByLabelText("Declaration tree")).toBeNull();
  });

  it("edits a collaborator from the friendly list and preserves its nested declaration", async () => {
    const env = setup();
    await screen.findByDisplayValue("Original");
    fireEvent.click(screen.getByRole("button", { name: "Edit helper" }));
    fireEvent.change(await screen.findByDisplayValue("nested"), { target: { value: "Research sources" } });
    expect(screen.queryByRole("button", { name: "Add collaborator" })).toBeNull();
    expect(await screen.findByRole("checkbox", { name: "research" })).toBeTruthy();
    fireEvent.click(screen.getByTestId("pools-save"));
    await waitFor(() => expect(env.writes).toContain("scope"));
    expect(JSON.stringify(env.tree())).toContain("Research sources");
    expect(JSON.stringify(env.tree())).toContain('"deeper":{"description":"deep"}');
  });

  it("discards only the outgoing Prompt draft, keeping Scope changes on selection", async () => {
    setup();
    await screen.findByDisplayValue("Original instructions");
    fireEvent.change(screen.getByDisplayValue("Original"), { target: { value: "Keep this Scope edit" } });
    fireEvent.change(screen.getByDisplayValue("Original instructions"), { target: { value: "Discard this Prompt edit" } });
    fireEvent.click(screen.getByRole("button", { name: /^other Other$/ }));
    fireEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Discard" }));
    fireEvent.click(screen.getByRole("button", { name: /^main.*Keep this Scope edit/ }));
    expect(await screen.findByDisplayValue("Keep this Scope edit")).toBeTruthy();
    expect(await screen.findByDisplayValue("Original instructions")).toBeTruthy();
  });

  it("creates a minimal assistant through the existing Scope save", async () => {
    const env = setup();
    await screen.findByDisplayValue("Original");
    fireEvent.click(screen.getByRole("button", { name: "New assistant" }));
    fireEvent.change(screen.getByLabelText("Key"), { target: { value: "researcher" } });
    fireEvent.click(screen.getByRole("button", { name: "Add" }));
    fireEvent.click(screen.getByTestId("pools-save"));
    await waitFor(() => expect(env.writes).toContain("scope"));
    const pools = (env.tree().workspace as { pools: Record<string, unknown> }).pools;
    expect(pools.researcher).toEqual({ agents: { researcher: { description: "", hooks: ["+session_title"] } } });
  });

  it("requires a saved replacement before deleting the default assistant", async () => {
    const env = setup();
    await screen.findByDisplayValue("Original");
    fireEvent.click(screen.getByRole("button", { name: "Delete main" }));
    expect(screen.getByRole("dialog").textContent).toContain("Choose another default");
    expect(env.writes).toEqual([]);
    expect(env.prefs().default_pool).toBe("main");
  });
});
