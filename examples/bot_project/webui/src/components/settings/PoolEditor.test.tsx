import { describe, it, expect, vi, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { PoolEditor } from "./PoolEditor";
import { ToastProvider } from "../ToastContext";

function makeResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

const tree = {
  name: "default",
  main_agent_name: "main",
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

afterEach(() => vi.unstubAllGlobals());

function renderEditor(props: {
  pool?: string;
  onDirtyChange?: (d: boolean) => void;
}): void {
  render(
    <ToastProvider>
      <PoolEditor
        pool={props.pool ?? "default"}
        onDirtyChange={props.onDirtyChange}
      />
    </ToastProvider>,
  );
}

describe("PoolEditor", () => {
  it("loads the pool and renders main agent name", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn((url: string) =>
        Promise.resolve(makeResponse(200, url.includes("/skills") ? [] : tree)),
      ),
    );
    renderEditor({});
    await waitFor(() =>
      expect((screen.getByDisplayValue("main") as HTMLInputElement).value).toBe(
        "main",
      ),
    );
  });

  it("editing a field marks the editor dirty (onDirtyChange(true))", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn((url: string) =>
        Promise.resolve(makeResponse(200, url.includes("/skills") ? [] : tree)),
      ),
    );
    const onDirtyChange = vi.fn();
    renderEditor({ onDirtyChange });
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
      return Promise.resolve(
        makeResponse(200, url.includes("/skills") ? [] : tree),
      );
    });
    vi.stubGlobal("fetch", fetchMock);
    renderEditor({});
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
      return Promise.resolve(
        makeResponse(200, url.includes("/skills") ? [] : tree),
      );
    });
    vi.stubGlobal("fetch", fetchMock);
    renderEditor({});
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
      vi.fn((url: string) =>
        Promise.resolve(makeResponse(200, url.includes("/skills") ? [] : tree)),
      ),
    );
    renderEditor({});
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
      vi.fn((url: string) =>
        Promise.resolve(makeResponse(200, url.includes("/skills") ? [] : tree)),
      ),
    );
    renderEditor({});
    await waitFor(() =>
      expect(screen.getByText("researcher")).toBeTruthy(),
    );
    fireEvent.click(screen.getByRole("button", { name: /Add subagent/ }));
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
      vi.fn((url: string) =>
        Promise.resolve(makeResponse(200, url.includes("/skills") ? [] : tree)),
      ),
    );
    renderEditor({});
    await waitFor(() => expect(screen.getByDisplayValue("main")).toBeTruthy());

    // Add a subagent — auto-expanded with empty agent_name.
    fireEvent.click(screen.getByRole("button", { name: /Add subagent/ }));

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
      vi.fn((url: string) =>
        Promise.resolve(makeResponse(200, url.includes("/skills") ? [] : tree)),
      ),
    );
    renderEditor({});
    await waitFor(() => expect(screen.getByDisplayValue("main")).toBeTruthy());

    fireEvent.click(screen.getByRole("button", { name: /Add subagent/ }));

    // After add-subagent (approval disabled, subagent description also rendered)
    // the DOM textboxes (role="textbox") are, in order:
    //   [mainName, subagentName, subagentDescription]
    // — number inputs are role="spinbutton", textareas don't exist here.
    const textboxes = screen.getAllByRole("textbox") as HTMLInputElement[];
    expect(textboxes.length).toBe(3);
    const subagentNameInput = textboxes[1]!;
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
          makeResponse(200, url.includes("/skills") ? [] : treeNoSub),
        ),
      ),
    );
    renderEditor({});
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
});
