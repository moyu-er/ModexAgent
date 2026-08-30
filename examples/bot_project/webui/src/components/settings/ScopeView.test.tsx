// ScopeView component test (ticket 16) — real TopologyCanvas rendering
// (structural assertions: node counts / level labels / peer edges), bill
// provenance display, and the PoolEditor-pattern write-back flow.

import { describe, it, expect, vi, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor, within } from "@testing-library/react";
import { ScopeView } from "./ScopeView";
import { ToastProvider } from "../ToastContext";

vi.mock("../graphs/yaml/YamlCodeEditor", () => ({
  YamlCodeEditor: ({
    value,
    onChange,
  }: {
    value: string;
    onChange?: (v: string) => void;
  }) => (
    <textarea
      data-testid="yaml-editor"
      value={value}
      onChange={(e) => onChange?.(e.target.value)}
    />
  ),
}));

function makeResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

const DECLARATION_YAML = "workspace:\n  name: bot\n";

const TOPOLOGY = {
  kind: "workspace",
  workspace: "bot",
  pools: [
    {
      name: "main",
      peers: ["helper"],
      agents: [
        { name: "main", parent: null, root: true },
        { name: "worker", parent: "main", root: false },
      ],
    },
    {
      name: "helper",
      peers: ["main"],
      agents: [{ name: "helper", parent: null, root: true }],
    },
  ],
};

const BILL = {
  agents: [
    {
      pool: "main",
      agent: "main",
      root: true,
      fields: [
        { field: "toolset", value: "full", layer: "framework", profile: null },
        { field: "max_steps", value: 50, layer: "local", profile: null },
      ],
      tools: [
        { tool: "read", origin: "preset", replaces: null, targets: [] },
        { tool: "edit", origin: "preset", replaces: null, targets: [] },
        { tool: "aci_edit", origin: "supplement", replaces: "edit", targets: [] },
        { tool: "task", origin: "derived_task", replaces: null, targets: ["worker"] },
      ],
      replacements: [
        { default_tool: "edit", replacement_tool: "aci_edit", supplement: "aci" },
      ],
    },
    {
      pool: "main",
      agent: "worker",
      root: false,
      fields: [
        { field: "toolset", value: "read_write", layer: "framework", profile: null },
        { field: "max_steps", value: 60, layer: "local", profile: null },
      ],
      tools: [
        { tool: "read", origin: "preset", replaces: null, targets: [] },
        {
          tool: "send_to_agent",
          origin: "derived_send_to_agent",
          replaces: null,
          targets: ["main"],
        },
      ],
      replacements: [],
    },
    {
      pool: "helper",
      agent: "helper",
      root: true,
      fields: [
        { field: "toolset", value: "full", layer: "framework", profile: null },
      ],
      tools: [
        {
          tool: "send_to_peer",
          origin: "derived_send_to_peer",
          replaces: null,
          targets: ["main"],
        },
      ],
      replacements: [],
    },
  ],
};

function defaultFetch(url: string, init?: RequestInit): Promise<Response> {
  const method = init?.method ?? "GET";
  if (url === "/api/scope/declaration" && method === "GET") {
    return Promise.resolve(makeResponse(200, { yaml: DECLARATION_YAML }));
  }
  if (url === "/api/scope/topology") {
    return Promise.resolve(makeResponse(200, TOPOLOGY));
  }
  if (url === "/api/scope/bill") {
    return Promise.resolve(makeResponse(200, BILL));
  }
  if (url === "/api/scope/declaration" && method === "PUT") {
    return Promise.resolve(makeResponse(200, { saved: true, restart_required: true }));
  }
  return Promise.resolve(makeResponse(404, { error: `unmocked ${method} ${url}` }));
}

async function renderView(fetchImpl = defaultFetch): Promise<void> {
  vi.stubGlobal("fetch", vi.fn(fetchImpl));
  render(
    <ToastProvider>
      <ScopeView />
    </ToastProvider>,
  );
  await waitFor(() => expect(screen.getByTestId("scope-view")).toBeTruthy());
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("ScopeView — declaration tree canvas", () => {
  it("renders every declared node with its level label (data-node-type)", async () => {
    await renderView();
    const expected: Record<string, string> = {
      bot: "workspace",
      main: "pool",
      helper: "pool",
      "main.main": "agent",
      "main.worker": "agent",
      "helper.helper": "agent",
    };
    for (const [id, nodeType] of Object.entries(expected)) {
      const el = screen.getByTestId(`graph-node-${id}`);
      expect(el.getAttribute("data-node-type")).toBe(nodeType);
    }
    // 1 workspace + 2 pools + 3 agents = 6 nodes, nothing else.
    expect(document.querySelectorAll("[data-node-type]")).toHaveLength(6);
  });

  it("renders containment, parent-derived, and deduped peer edges", async () => {
    await renderView();
    for (const key of [
      "bot-main",
      "bot-helper",
      "main-main.main",
      "main.main-main.worker",
      "helper-helper.helper",
      "main-helper", // peer link, rendered once (first declaration order)
    ]) {
      expect(screen.getByTestId(`graph-edge-${key}`)).toBeTruthy();
    }
    // The reverse direction of the bidirectional peer pair is not rendered.
    expect(screen.queryByTestId("graph-edge-helper-main")).toBeNull();
  });
});

describe("ScopeView — provenance bill", () => {
  it("shows per-field source layers and values", async () => {
    await renderView();
    const card = screen.getByTestId("scope-bill-agent-main-main");
    expect(card).toBeTruthy();
    const maxSteps = within(card).getByTestId("scope-bill-field-max_steps");
    expect(maxSteps.getAttribute("data-layer")).toBe("local");
    expect(maxSteps.textContent).toContain("50");
    const toolset = within(card).getByTestId("scope-bill-field-toolset");
    expect(toolset.getAttribute("data-layer")).toBe("framework");
    expect(toolset.textContent).toContain("full");
    // The non-root agent's bill renders the same field keys in its own card.
    const workerCard = screen.getByTestId("scope-bill-agent-main-worker");
    const workerToolset = within(workerCard).getByTestId("scope-bill-field-toolset");
    expect(workerToolset.textContent).toContain("read_write");
  });

  it("shows component implementation sources (O2/O3 audit surface)", async () => {
    await renderView();
    const card = screen.getByTestId("scope-bill-agent-main-main");
    const aciEdit = within(card).getByTestId("scope-bill-tool-aci_edit");
    expect(aciEdit.getAttribute("data-origin")).toBe("supplement");
    expect(aciEdit.textContent).toContain("← edit");
    const presetRead = within(card).getByTestId("scope-bill-tool-read");
    expect(presetRead.getAttribute("data-origin")).toBe("preset");
    expect(
      within(card).getByTestId("scope-bill-replacement-edit").textContent,
    ).toBe("edit ← aci_edit (aci)");
    const task = within(card).getByTestId("scope-bill-tool-task");
    expect(task.getAttribute("data-origin")).toBe("derived_task");
    expect(task.textContent).toContain("→ worker");
  });
});

describe("ScopeView — write-back (PoolEditor pattern)", () => {
  it("save PUTs the edited YAML, surfaces the restart toast, and refetches", async () => {
    const fetchMock = vi.fn(defaultFetch);
    vi.stubGlobal("fetch", fetchMock);
    render(
      <ToastProvider>
        <ScopeView />
      </ToastProvider>,
    );
    await waitFor(() => expect(screen.getByTestId("scope-view")).toBeTruthy());

    fireEvent.change(screen.getByTestId("yaml-editor"), {
      target: { value: "workspace:\n  name: renamed\n" },
    });
    fireEvent.click(screen.getByTestId("scope-save"));

    await waitFor(() =>
      expect(screen.getByText("Saved. Restart to apply.")).toBeTruthy(),
    );
    type Call = [unknown, RequestInit?];
    const calls = fetchMock.mock.calls as unknown as Call[];
    const puts = calls.filter(
      (c) => c[0] === "/api/scope/declaration" && c[1]?.method === "PUT",
    );
    expect(puts).toHaveLength(1);
    expect(JSON.parse(String(puts[0]![1]!.body))).toEqual({
      yaml: "workspace:\n  name: renamed\n",
    });
    // Bill + topology refetched after the write (unrestarted edit shows as
    // the on-disk declaration).
    const billGets = calls.filter((c) => c[0] === "/api/scope/bill");
    expect(billGets.length).toBeGreaterThanOrEqual(2);
  });

  it("save failure surfaces the backend validation issues", async () => {
    const fetchMock = vi.fn((url: string, init?: RequestInit) => {
      if (url === "/api/scope/declaration" && init?.method === "PUT") {
        return Promise.resolve(
          makeResponse(400, {
            error: "declaration invalid",
            issues: [{ rule: "V3", node: "main", message: "expected one root" }],
          }),
        );
      }
      return defaultFetch(url, init);
    });
    await renderView(fetchMock);
    fireEvent.change(screen.getByTestId("yaml-editor"), {
      target: { value: "pool:\n  name: broken\n" },
    });
    fireEvent.click(screen.getByTestId("scope-save"));
    await waitFor(() => {
      const banner = screen.getByTestId("scope-save-error");
      expect(banner.textContent).toContain("V3 main: expected one root");
    });
  });
});
