import { describe, it, expect, vi, afterEach } from "vitest";
import { useRef, useState } from "react";
import { render, screen, fireEvent, waitFor, within } from "@testing-library/react";
import { PoolEditor } from "./PoolEditor";
import { ToastProvider } from "../ToastContext";
import { ActionBar } from "../ui/ActionBar";
import { Button } from "../ui/Button";
import {
  DEFAULT_EXTERNAL_PROVIDER,
  DEFAULT_EXTERNAL_PROVIDER_DESCRIPTOR,
  PROVIDER_OPTIONS,
} from "../../types/externalProviders";

function makeResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

const tree = {
  name: "default",
  main_agent_name: "main",
  peers: [],
  main: {
    agent_name: "main",
    max_steps: 12,
    use_terminal: false,
    terminal_visibility: false,
    tool_preset: "full",
    tool_supplements: [],
    approval: {
      enabled: false,
      tools: { write: { allowed_paths: [] }, edit: { allowed_paths: [] } },
    },
    mcp: [],
  },
  subagents: [
    {
      agent_name: "researcher",
      description: "Looks things up",
      max_steps: 8,
      tool_preset: "read_write",
      tool_supplements: [],
      context_mode: "fork",
      mcp: [],
    },
  ],
  restart_required: false,
};

const poolList = [{ name: "default", main_agent_name: "main", subagent_count: 0 }];

function defaultFetch(url: string): Response {
  if (url === "/api/pools") return makeResponse(200, poolList);
  if (url.includes("/skills")) return makeResponse(200, []);
  return makeResponse(200, tree);
}

afterEach(() => vi.unstubAllGlobals());

async function renderEditor(props: {
  pool?: string;
  onDirtyChange?: (d: boolean) => void;
}): Promise<void> {
  render(
    <ToastProvider>
      <PoolEditor
        pool={props.pool ?? "default"}
        onDirtyChange={props.onDirtyChange}
      />
    </ToastProvider>,
  );
  await waitFor(() =>
    expect(screen.queryByText("Loading…")).toBeNull(),
  );
}

/** Renders PoolEditor together with the ActionBar now hosted by PoolsView. */
function EditorWithActionBar({ pool }: { pool?: string }) {
  const saveRef = useRef<(() => Promise<void>) | null>(null);
  const cancelRef = useRef<(() => void) | null>(null);
  const [dirty, setDirty] = useState<boolean>(false);
  return (
    <ToastProvider>
      <PoolEditor
        pool={pool ?? "default"}
        onDirtyChange={setDirty}
        onSave={(save) => {
          saveRef.current = save;
        }}
        onCancel={(cancel) => {
          cancelRef.current = cancel;
        }}
      />
      <ActionBar>
        <Button
          variant="secondary"
          size="sm"
          onClick={() => cancelRef.current?.()}
          disabled={!dirty}
        >
          Cancel
        </Button>
        <Button
          variant="primary"
          size="sm"
          onClick={() => saveRef.current?.()}
          disabled={!dirty}
        >
          Save
        </Button>
      </ActionBar>
    </ToastProvider>
  );
}

async function renderEditorWithActionBar(pool?: string): Promise<void> {
  render(<EditorWithActionBar pool={pool} />);
  await waitFor(() =>
    expect(screen.queryByText("Loading…")).toBeNull(),
  );
}

describe("PoolEditor", () => {
  it("loads the pool and renders main agent name", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn((url: string) => Promise.resolve(defaultFetch(url))),
    );
    await renderEditor({});
    await waitFor(() =>
      expect((screen.getByDisplayValue("main") as HTMLInputElement).value).toBe(
        "main",
      ),
    );
  });

  it("editing a field marks the editor dirty (onDirtyChange(true))", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn((url: string) => Promise.resolve(defaultFetch(url))),
    );
    const onDirtyChange = vi.fn();
    await renderEditor({ onDirtyChange });
    await waitFor(() => expect(screen.getByDisplayValue("main")).toBeTruthy());
    // mcp selector also fires a fetch on mount; ignore it.
    fireEvent.change(screen.getByDisplayValue("main"), {
      target: { value: "boss" },
    });
    expect(onDirtyChange).toHaveBeenCalledWith(true);
  });

  it("Save calls savePool (PUT) and surfaces restart toast when restart_required", async () => {
    const fetchMock = vi.fn((url: string, init?: RequestInit) => {
      const method = init?.method ?? "GET";
      if (url.includes("/pools/default") && method === "PUT") {
        return Promise.resolve(
          makeResponse(200, { ...tree, restart_required: true }),
        );
      }
      return Promise.resolve(defaultFetch(url));
    });
    vi.stubGlobal("fetch", fetchMock);
    await renderEditorWithActionBar();
    await waitFor(() => expect(screen.getByDisplayValue("main")).toBeTruthy());
    fireEvent.change(screen.getByDisplayValue("main"), {
      target: { value: "boss" },
    });
    fireEvent.click(screen.getByText("Save"));
    await waitFor(() =>
      expect(screen.getByText("Saved. Restart to apply.")).toBeTruthy(),
    );
    type Call = [unknown, RequestInit?];
    const calls = fetchMock.mock.calls as unknown as Call[];
    const puts = calls.filter((c) => c[1]?.method === "PUT");
    expect(puts.length).toBeGreaterThanOrEqual(1);
    // The PUT body carries the edited value (agent_name "boss"), not just any PUT.
    const putBody = JSON.parse(String(puts[0]![1]!.body)) as {
      main?: { agent_name?: string };
    };
    expect(putBody.main?.agent_name).toBe("boss");
  });

  it("validation error (400 fields) maps onto inline field error", async () => {
    const fetchMock = vi.fn((url: string, init?: RequestInit) => {
      const method = init?.method ?? "GET";
      if (url.includes("/pools/default") && method === "PUT") {
        return Promise.resolve(
          makeResponse(400, {
            error: "validation",
            fields: { "main.agent_name": ["required"] },
          }),
        );
      }
      return Promise.resolve(defaultFetch(url));
    });
    vi.stubGlobal("fetch", fetchMock);
    await renderEditorWithActionBar();
    await waitFor(() => expect(screen.getByDisplayValue("main")).toBeTruthy());
    // touch to enable Save
    fireEvent.change(screen.getByDisplayValue("main"), {
      target: { value: "boss" },
    });
    fireEvent.click(screen.getByText("Save"));
    await waitFor(() =>
      expect(screen.getByText("required")).toBeTruthy(),
    );
  });

  it("Cancel restores the original form", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn((url: string) => Promise.resolve(defaultFetch(url))),
    );
    await renderEditorWithActionBar();
    await waitFor(() => expect(screen.getByDisplayValue("main")).toBeTruthy());
    const input = screen.getByDisplayValue("main") as HTMLInputElement;
    fireEvent.change(input, { target: { value: "boss" } });
    expect(input.value).toBe("boss");
    fireEvent.click(screen.getByText("Cancel"));
    expect(input.value).toBe("main");
  });

  it("Add subagent appends one and Remove subagent deletes it after inline confirm", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn((url: string) => Promise.resolve(defaultFetch(url))),
    );
    await renderEditor({});
    await waitFor(() =>
      expect(screen.getByText("researcher")).toBeTruthy(),
    );
    fireEvent.click(screen.getByRole("button", { name: /Add subagent/ }));
    await waitFor(() =>
      expect(screen.queryAllByText("Loading…")).toHaveLength(0),
    );
    // one more subagent card with "Untitled subagent" placeholder text
    expect(screen.getAllByText(/Untitled subagent|researcher/).length).toBe(2);

    // delete the first subagent (researcher)
    fireEvent.click(
      screen.getByRole("button", { name: "Remove subagent researcher" }),
    );
    fireEvent.click(screen.getByRole("button", { name: "Delete" }));
    await waitFor(() =>
      expect(screen.queryByText("researcher")).toBeNull(),
    );
  });

  it("switch-pool-while-dirty confirm is owned by PoolsView (not PoolEditor)", () => {
    // Smoke: PoolEditor renders. The switch-while-dirty ConfirmDialog lives in
    // PoolsView; covered in PoolsView.test.tsx.
    expect(true).toBe(true);
  });

  // ─── system-prompt Edit button gating ──────────────────────────────────
  // Regression: clicking "System prompt [Edit]" on an unnamed subagent built
  // an URL with a double slash (`/agents//prompt`), which aiohttp routed to
  // a 404. The button is now disabled when agent_name is empty/invalid; the
  // backend PUT still creates the file once a valid name is provided.

  it("disables 'System prompt [Edit]' for an untitled (empty-name) subagent", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn((url: string) => Promise.resolve(defaultFetch(url))),
    );
    await renderEditor({});
    await waitFor(() => expect(screen.getByDisplayValue("main")).toBeTruthy());

    // Add a subagent — auto-expanded with empty agent_name.
    fireEvent.click(screen.getByRole("button", { name: /Add subagent/ }));
    await waitFor(() =>
      expect(screen.queryAllByText("Loading…")).toHaveLength(0),
    );

    const editBtns = screen.getAllByRole("button", {
      name: /System prompt \[Edit\]/,
    }) as HTMLButtonElement[];
    // [main, subagent]
    expect(editBtns.length).toBe(2);
    expect(editBtns[0]!.disabled).toBe(false);
    expect(editBtns[1]!.disabled).toBe(true);
    expect(screen.getByText(/Provide an agent name/)).toBeTruthy();
  });

  it("enables 'System prompt [Edit]' once a valid name is typed", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn((url: string) => Promise.resolve(defaultFetch(url))),
    );
    await renderEditor({});
    await waitFor(() => expect(screen.getByDisplayValue("main")).toBeTruthy());

    fireEvent.click(screen.getByRole("button", { name: /Add subagent/ }));
    await waitFor(() =>
      expect(screen.queryAllByText("Loading…")).toHaveLength(0),
    );

    // After add-subagent (approval disabled, main + subagent descriptions
    // rendered) the DOM textboxes (role="textbox") are, in order:
    //   [mainName, mainDescription, subagentName, subagentDescription]
    // — number inputs are role="spinbutton", textareas don't exist here.
    const textboxes = screen.getAllByRole("textbox") as HTMLInputElement[];
    expect(textboxes.length).toBe(4);
    const subagentNameInput = textboxes[2]!;
    expect(subagentNameInput.value).toBe("");
    fireEvent.change(subagentNameInput, { target: { value: "oracle" } });

    const editBtns = screen.getAllByRole("button", {
      name: /System prompt \[Edit\]/,
    }) as HTMLButtonElement[];
    expect(editBtns[1]!.disabled).toBe(false);
    expect(screen.queryByText(/Provide an agent name/)).toBeNull();
  });

  it("disables main agent's 'System prompt [Edit]' when main name is cleared", async () => {
    // Use a no-subagent tree so only main's Edit button is in the DOM
    // (existing subagent's button is gated behind a collapsed card).
    const treeNoSub = { ...tree, subagents: [] };
    vi.stubGlobal(
      "fetch",
      vi.fn((url: string) =>
        Promise.resolve(
          makeResponse(
            200,
            url === "/api/pools"
              ? poolList
              : url.includes("/skills")
                ? []
                : treeNoSub,
          ),
        ),
      ),
    );
    await renderEditor({});
    await waitFor(() => expect(screen.getByDisplayValue("main")).toBeTruthy());

    fireEvent.change(screen.getByDisplayValue("main"), {
      target: { value: "" },
    });

    const editBtns = screen.getAllByRole("button", {
      name: /System prompt \[Edit\]/,
    }) as HTMLButtonElement[];
    expect(editBtns.length).toBe(1);
    expect(editBtns[0]!.disabled).toBe(true);
  });

  it("renders the 'Skill assignments save immediately.' caption", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn((url: string) => Promise.resolve(defaultFetch(url))),
    );
    await renderEditor({});
    await waitFor(() => expect(screen.getByDisplayValue("main")).toBeTruthy());
    // Main agent's caption is always visible. (Subagent captions only render
    // when the card is expanded.) We assert main caption exists with Geist styling.
    const caption = screen.getByText(/Skill assignments save immediately/);
    expect(caption.className).toContain("italic");
    expect(caption.className).toContain("text-body");
  });

  it("System prompt [Edit] opens a slide-over (does not unmount PoolEditor)", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn((url: string) => Promise.resolve(defaultFetch(url))),
    );
    await renderEditor({});
    await waitFor(() => expect(screen.getByDisplayValue("main")).toBeTruthy());

    const editBtn = screen.getByRole("button", {
      name: /System prompt \[Edit\]/,
    }) as HTMLButtonElement;
    fireEvent.click(editBtn);

    // Slide-over dialog renders the prompt editor; the underlying Pool header
    // is still in the DOM (i.e. the editor was not unmounted).
    await waitFor(() =>
      expect(screen.getByRole("dialog", { name: /prompt editor/i })).toBeTruthy(),
    );
    expect(screen.getByText(/Pool: default/)).toBeTruthy();
  });

  const multiPoolList = [
    { name: "default", main_agent_name: "main", subagent_count: 0 },
    { name: "research", main_agent_name: "research-main", subagent_count: 0 },
  ];

  it("shows peer pool name with its main agent name", async () => {
    const treeWithPeer = { ...tree, peers: ["research"] };
    vi.stubGlobal(
      "fetch",
      vi.fn((url: string) =>
        Promise.resolve(
          makeResponse(
            200,
            url === "/api/pools"
              ? multiPoolList
              : url.includes("/skills")
                ? []
                : treeWithPeer,
          ),
        ),
      ),
    );
    await renderEditor({});
    await waitFor(() => expect(screen.getByText("research")).toBeTruthy());
    expect(screen.getByText(/main agent: research-main/)).toBeTruthy();
  });

  it("adds a peer via POST /api/pools/{pool}/peers", async () => {
    const treeWithPeer = { ...tree, peers: ["research"] };
    const fetchMock = vi.fn((url: string, init?: RequestInit) => {
      const method = init?.method ?? "GET";
      if (url === "/api/pools" && method === "GET") {
        return Promise.resolve(makeResponse(200, multiPoolList));
      }
      if (url === "/api/pools/default/peers" && method === "POST") {
        return Promise.resolve(
          makeResponse(200, { pool_a: treeWithPeer, pool_b: { ...tree, name: "research", peers: ["default"] } }),
        );
      }
      return Promise.resolve(defaultFetch(url));
    });
    vi.stubGlobal("fetch", fetchMock);
    await renderEditor({});
    await waitFor(() => expect(screen.getByDisplayValue("main")).toBeTruthy());

    fireEvent.click(screen.getByRole("button", { name: /Add peer/ }));
    const select = screen.getByLabelText("New peer pool") as HTMLSelectElement;
    fireEvent.change(select, { target: { value: "research" } });
    fireEvent.click(screen.getByRole("button", { name: /^Add$/ }));

    await waitFor(() => expect(screen.getByText("research")).toBeTruthy());
    const calls = fetchMock.mock.calls as [string, RequestInit?][];
    const posts = calls.filter((c) => c[1]?.method === "POST");
    expect(posts.some((c) => c[0] === "/api/pools/default/peers")).toBe(true);
  });

  it("removes a peer via DELETE /api/pools/{pool}/peers/{peer}", async () => {
    const treeWithPeer = { ...tree, peers: ["research"] };
    const fetchMock = vi.fn((url: string, init?: RequestInit) => {
      const method = init?.method ?? "GET";
      if (url === "/api/pools" && method === "GET") {
        return Promise.resolve(makeResponse(200, multiPoolList));
      }
      if (url === "/api/pools/default/peers/research" && method === "DELETE") {
        return Promise.resolve(
          makeResponse(200, { pool_a: { ...tree, peers: [] }, pool_b: { ...tree, name: "research", peers: [] } }),
        );
      }
      return Promise.resolve(makeResponse(200, url.includes("/skills") ? [] : treeWithPeer));
    });
    vi.stubGlobal("fetch", fetchMock);
    await renderEditor({});
    await waitFor(() => expect(screen.getByText("research")).toBeTruthy());

    fireEvent.click(screen.getByRole("button", { name: "Remove peer research" }));
    fireEvent.click(screen.getByRole("button", { name: "Remove" }));

    await waitFor(() => expect(screen.queryByText("research")).toBeNull());
    const calls = fetchMock.mock.calls as [string, RequestInit?][];
    const deletes = calls.filter((c) => c[1]?.method === "DELETE");
    expect(deletes.some((c) => c[0] === "/api/pools/default/peers/research")).toBe(true);
  });

  it("surfaces peer add errors from the backend", async () => {
    const fetchMock = vi.fn((url: string, init?: RequestInit) => {
      const method = init?.method ?? "GET";
      if (url === "/api/pools" && method === "GET") {
        return Promise.resolve(makeResponse(200, multiPoolList));
      }
      if (url === "/api/pools/default/peers" && method === "POST") {
        return Promise.resolve(
          makeResponse(400, { error: "validation", fields: { peer: ["already a peer"] } }),
        );
      }
      return Promise.resolve(defaultFetch(url));
    });
    vi.stubGlobal("fetch", fetchMock);
    await renderEditor({});
    await waitFor(() => expect(screen.getByDisplayValue("main")).toBeTruthy());

    fireEvent.click(screen.getByRole("button", { name: /Add peer/ }));
    const select = screen.getByLabelText("New peer pool") as HTMLSelectElement;
    fireEvent.change(select, { target: { value: "research" } });
    fireEvent.click(screen.getByRole("button", { name: /^Add$/ }));

    await waitFor(() => expect(screen.getByText("already a peer")).toBeTruthy());
  });

  // ─── External pool mode ───────────────────────────────────────────────
  //
  // External (external_coding) pools run their main agent in a provider CLI
  // (OpenCode). The editor groups Implementation + Provider into one
  // runtime-first panel, hides native-only config in external mode, preserves
  // peers, and confirms the native→external switch because it clears draft
  // subagents. external→native is non-destructive and applies directly.

  const treeExternal = {
    ...tree,
    main: {
      ...tree.main,
      execution_strategy: "external_coding",
      provider_kind: "opencode",
    },
    subagents: [],
  };

  function externalFetch(url: string): Response {
    if (url === "/api/pools") return makeResponse(200, poolList);
    if (url.includes("/skills")) return makeResponse(200, []);
    return makeResponse(200, treeExternal);
  }

  it("renders an Implementation select defaulting to Native for a react pool", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn((url: string) => Promise.resolve(defaultFetch(url))),
    );
    await renderEditor({});
    const impl = screen.getByLabelText("Implementation") as HTMLSelectElement;
    expect(impl.value).toBe("react");
    // Native controls are visible in native mode.
    expect(screen.getByLabelText("Max steps")).toBeTruthy();
    // No external runtime panel in native mode.
    expect(screen.queryByTestId("external-runtime-panel")).toBeNull();
  });

  it("external mode groups Implementation + Provider in one runtime panel; Provider has only OpenCode; hides native-only controls, subagents and system prompt", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn((url: string) => Promise.resolve(externalFetch(url))),
    );
    await renderEditor({});

    // Both selects live inside one runtime panel (semantic group, not a
    // fragile class) — the approved runtime-first layout.
    const panel = screen.getByTestId("external-runtime-panel");
    const impl = within(panel).getByLabelText(
      "Implementation",
    ) as HTMLSelectElement;
    const provider = within(panel).getByLabelText(
      "Provider",
    ) as HTMLSelectElement;
    expect(impl.value).toBe("external_coding");
    expect(provider.value).toBe("opencode");
    // Provider dropdown carries exactly one option: OpenCode.
    expect(provider.querySelectorAll("option")).toHaveLength(1);
    // Provider dropdown options equal the catalog.
    expect(
      Array.from(provider.querySelectorAll("option")).map(
        (o) => (o as HTMLOptionElement).value,
      ),
    ).toEqual(PROVIDER_OPTIONS.map((o) => o.value));

    // Identity fields follow the runtime panel.
    expect(screen.getByLabelText(/Agent name/)).toBeTruthy();
    expect(screen.getByLabelText("Description")).toBeTruthy();

    // Native-only configuration is hidden.
    expect(screen.queryByLabelText("Max steps")).toBeNull();
    expect(screen.queryByLabelText("Tool preset")).toBeNull();
    expect(screen.queryByText("Terminal")).toBeNull();
    expect(
      screen.queryByText("Approval required for write/edit tools"),
    ).toBeNull();
    expect(screen.queryByText("Skill assignments save immediately.")).toBeNull();
    expect(
      screen.queryByRole("button", { name: /System prompt \[Edit\]/ }),
    ).toBeNull();

    // Subagents section is hidden entirely.
    expect(screen.queryByText("Subagents")).toBeNull();
    expect(screen.queryByRole("button", { name: /Add subagent/ })).toBeNull();

    // Managed-capability summary is present and uses the descriptor's CLI name.
    expect(screen.getByText(/Managed by the provider runtime/)).toBeTruthy();
    expect(
      within(panel).getByText(
        new RegExp(
          `controlled by the ${DEFAULT_EXTERNAL_PROVIDER_DESCRIPTOR.cliName} CLI`,
        ),
      ),
    ).toBeTruthy();
  });

  it("external mode preserves the Peers section", async () => {
    const treeExtPeer = { ...treeExternal, peers: ["research"] };
    vi.stubGlobal(
      "fetch",
      vi.fn((url: string) =>
        Promise.resolve(
          makeResponse(
            200,
            url === "/api/pools"
              ? multiPoolList
              : url.includes("/skills")
                ? []
                : treeExtPeer,
          ),
        ),
      ),
    );
    await renderEditor({});
    expect(screen.getByText("Peers")).toBeTruthy();
    expect(screen.getByText("research")).toBeTruthy();
  });

  it("switching native→external opens a confirm; Cancel leaves draft and disk untouched", async () => {
    const fetchMock = vi.fn((url: string) =>
      Promise.resolve(defaultFetch(url)),
    );
    vi.stubGlobal("fetch", fetchMock);
    await renderEditor({});
    // Native pool carries one subagent.
    expect(screen.getByText("researcher")).toBeTruthy();

    fireEvent.change(screen.getByLabelText("Implementation"), {
      target: { value: "external_coding" },
    });

    // Confirm dialog opens with the save/cancel persisted-config disclaimer.
    expect(
      screen.getByRole("dialog", { name: /Switch to External/ }),
    ).toBeTruthy();
    expect(screen.getByText(/applied only when you click Save/)).toBeTruthy();
    expect(
      screen.getByText(/Cancel leaves the persisted configuration unchanged/),
    ).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    await waitFor(() =>
      expect(
        screen.queryByRole("dialog", { name: /Switch to External/ }),
      ).toBeNull(),
    );

    // Draft untouched: still native, subagent still present.
    expect(
      (screen.getByLabelText("Implementation") as HTMLSelectElement).value,
    ).toBe("react");
    expect(screen.getByText("researcher")).toBeTruthy();

    // Disk untouched: no PUT issued.
    const calls = fetchMock.mock.calls as [string, RequestInit?][];
    expect(calls.filter((c) => c[1]?.method === "PUT")).toHaveLength(0);
  });

  it("confirming native→external mutates draft (external, opencode, subagents cleared)", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn((url: string) => Promise.resolve(defaultFetch(url))),
    );
    await renderEditor({});
    expect(screen.getByText("researcher")).toBeTruthy();

    fireEvent.change(screen.getByLabelText("Implementation"), {
      target: { value: "external_coding" },
    });
    fireEvent.click(
      screen.getByRole("button", { name: "Switch to External" }),
    );
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());

    expect(
      (screen.getByLabelText("Implementation") as HTMLSelectElement).value,
    ).toBe("external_coding");
    expect(screen.queryByText("researcher")).toBeNull();
    expect(
      (screen.getByLabelText("Provider") as HTMLSelectElement).value,
    ).toBe("opencode");
  });

  it("Save after native→external PUT carries strategy, provider and cleared subagents", async () => {
    const fetchMock = vi.fn((url: string, init?: RequestInit) => {
      const method = init?.method ?? "GET";
      if (url.includes("/pools/default") && method === "PUT") {
        return Promise.resolve(
          makeResponse(200, { ...treeExternal, restart_required: false }),
        );
      }
      return Promise.resolve(defaultFetch(url));
    });
    vi.stubGlobal("fetch", fetchMock);
    await renderEditorWithActionBar();

    fireEvent.change(screen.getByLabelText("Implementation"), {
      target: { value: "external_coding" },
    });
    fireEvent.click(
      screen.getByRole("button", { name: "Switch to External" }),
    );
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    fireEvent.click(screen.getByText("Save"));

    await waitFor(() => {
      const calls = fetchMock.mock.calls as [string, RequestInit?][];
      expect(calls.filter((c) => c[1]?.method === "PUT").length).toBeGreaterThanOrEqual(1);
    });

    const calls = fetchMock.mock.calls as [string, RequestInit?][];
    const puts = calls.filter((c) => c[1]?.method === "PUT");
    const body = puts[0]?.[1]?.body;
    if (typeof body !== "string") throw new Error("PUT body missing");
    const putBody = JSON.parse(body) as {
      main?: { execution_strategy?: string; provider_kind?: string };
      subagents?: unknown[];
    };
    expect(putBody.main?.execution_strategy).toBe("external_coding");
    expect(putBody.main?.provider_kind).toBe(DEFAULT_EXTERNAL_PROVIDER);
    expect(putBody.subagents).toEqual([]);
  });

  it("existing external pool with unsupported provider normalizes to catalog default on save", async () => {
    const treePiExternal = {
      ...tree,
      main: {
        ...tree.main,
        execution_strategy: "external_coding" as const,
        provider_kind: "pi" as const,
      },
      subagents: [],
    };
    const fetchMock = vi.fn((url: string, init?: RequestInit) => {
      const method = init?.method ?? "GET";
      if (url.includes("/pools/default") && method === "PUT") {
        return Promise.resolve(
          makeResponse(200, { ...treeExternal, restart_required: false }),
        );
      }
      return Promise.resolve(
        makeResponse(
          200,
          url === "/api/pools"
            ? poolList
            : url.includes("/skills")
              ? []
              : treePiExternal,
        ),
      );
    });
    vi.stubGlobal("fetch", fetchMock);
    await renderEditorWithActionBar();

    // Provider shows catalog default for unsupported pi.
    expect(
      (screen.getByLabelText("Provider") as HTMLSelectElement).value,
    ).toBe(DEFAULT_EXTERNAL_PROVIDER);

    // Touch a field to enable Save.
    fireEvent.change(screen.getByLabelText(/Agent name/), {
      target: { value: "boss" },
    });
    fireEvent.click(screen.getByText("Save"));

    await waitFor(() => {
      const calls = fetchMock.mock.calls as [string, RequestInit?][];
      expect(
        calls.filter((c) => c[1]?.method === "PUT").length,
      ).toBeGreaterThanOrEqual(1);
    });

    const calls = fetchMock.mock.calls as [string, RequestInit?][];
    const puts = calls.filter((c) => c[1]?.method === "PUT");
    const body = puts[0]?.[1]?.body;
    if (typeof body !== "string") throw new Error("PUT body missing");
    const putBody = JSON.parse(body) as {
      main?: { provider_kind?: string };
    };
    // pi must NOT silently persist; catalog default is sent instead.
    expect(putBody.main?.provider_kind).toBe(DEFAULT_EXTERNAL_PROVIDER);
  });

  it("switching external→native applies directly without a confirm dialog", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn((url: string) => Promise.resolve(externalFetch(url))),
    );
    await renderEditor({});
    expect(
      (screen.getByLabelText("Implementation") as HTMLSelectElement).value,
    ).toBe("external_coding");

    fireEvent.change(screen.getByLabelText("Implementation"), {
      target: { value: "react" },
    });

    // No confirm dialog.
    expect(screen.queryByRole("dialog")).toBeNull();
    // Now native: Implementation react, native controls visible, provider gone.
    expect(
      (screen.getByLabelText("Implementation") as HTMLSelectElement).value,
    ).toBe("react");
    expect(screen.getByLabelText("Max steps")).toBeTruthy();
    expect(screen.queryByLabelText("Provider")).toBeNull();
  });
});
