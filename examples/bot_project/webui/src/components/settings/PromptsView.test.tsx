import { describe, it, expect, vi, afterEach } from "vitest";
import type { Mock } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { PromptsView } from "./PromptsView";
import { ToastProvider } from "../ToastContext";

function makeResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

afterEach(() => vi.unstubAllGlobals());

function renderView(): void {
  render(
    <ToastProvider>
      <PromptsView />
    </ToastProvider>,
  );
}

/** Build a fetch mock that handles list, get, put, post, delete, and restart URLs. */
function makeFetchMock(handlers: {
  list?: unknown[];
  get?: Record<string, { name: string; content: string }>;
  putStatus?: number;
  putBody?: { name: string; content: string };
  postStatus?: number;
  postBody?: { name: string; content: string };
  postedNames?: string[];
  deleteStatus?: number;
  deleteUsages?: { pool: string; agent_kind: "main" | "subagent"; agent_name: string }[];
  deletedNames?: string[];
}): Mock {
  const postedNames = handlers.postedNames ?? [];
  const deletedNames = handlers.deletedNames ?? [];
  return vi.fn((url: string, options?: RequestInit) => {
    const method = options?.method ?? "GET";
    if (url === "/api/prompts" && method === "GET") {
      return Promise.resolve(makeResponse(200, handlers.list ?? []));
    }
    if (url === "/api/prompts" && method === "POST") {
      const body = JSON.parse(options?.body as string) as { name: string };
      postedNames.push(body.name);
      if ((handlers.postStatus ?? 201) === 409) {
        return Promise.resolve(
          makeResponse(409, { error: "exists", name: body.name }),
        );
      }
      return Promise.resolve(
        makeResponse(
          handlers.postStatus ?? 201,
          handlers.postBody ?? {
            name: body.name,
            content: "seeded default",
          },
        ),
      );
    }
    if (url.startsWith("/api/prompts/") && method === "GET") {
      const name = decodeURIComponent(url.split("/api/prompts/")[1]!);
      const entry = (handlers.get ?? {})[name];
      if (!entry) {
        return Promise.resolve(makeResponse(404, { error: "unknown" }));
      }
      return Promise.resolve(makeResponse(200, entry));
    }
    if (url.startsWith("/api/prompts/") && method === "PUT") {
      const body = JSON.parse(options?.body as string) as { content: string };
      const name = decodeURIComponent(url.split("/api/prompts/")[1]!);
      return Promise.resolve(
        makeResponse(handlers.putStatus ?? 200, {
          name,
          content: body.content,
        }),
      );
    }
    if (url.startsWith("/api/prompts/") && method === "DELETE") {
      const name = decodeURIComponent(url.split("/api/prompts/")[1]!);
      if ((handlers.deleteStatus ?? 200) === 409) {
        return Promise.resolve(
          makeResponse(409, {
            error: "in_use",
            usages: handlers.deleteUsages ?? [],
          }),
        );
      }
      deletedNames.push(name);
      return Promise.resolve(makeResponse(200, { deleted: name }));
    }
    if (url === "/api/system/restart") {
      return Promise.resolve(makeResponse(200, { restarting: true }));
    }
    return Promise.resolve(makeResponse(404, { error: "unhandled" }));
  });
}

describe("PromptsView", () => {
  it("renders the prompt list from listPrompts", async () => {
    vi.stubGlobal(
      "fetch",
      makeFetchMock({
        list: [
          { name: "alpha", size_bytes: 100, mtime: "2026-01-01T00:00:00+00:00" },
          { name: "main", size_bytes: 200, mtime: "2026-01-02T00:00:00+00:00" },
        ],
      }),
    );
    renderView();
    await waitFor(() => expect(screen.getByText("alpha")).toBeTruthy());
    expect(screen.getByText("main")).toBeTruthy();
  });

  it("selecting a prompt loads its content via GET /api/prompts/{name}", async () => {
    vi.stubGlobal(
      "fetch",
      makeFetchMock({
        list: [
          { name: "main", size_bytes: 200, mtime: "2026-01-02T00:00:00+00:00" },
        ],
        get: {
          main: { name: "main", content: "You are the main agent." },
        },
      }),
    );
    renderView();
    await waitFor(() => expect(screen.getByText("main")).toBeTruthy());
    fireEvent.click(screen.getByText("main"));
    await waitFor(() =>
      expect((screen.getByRole("textbox") as HTMLTextAreaElement).value).toBe(
        "You are the main agent.",
      ),
    );
    // Editor is now editable: a Save button is present (disabled until dirty).
    expect(screen.getByText("Save")).toBeTruthy();
  });

  it("renders empty-state message when the list is empty", async () => {
    vi.stubGlobal("fetch", makeFetchMock({ list: [] }));
    renderView();
    await waitFor(() =>
      expect(screen.getByText("No prompts found.")).toBeTruthy(),
    );
  });

  it("shows the New prompt button even when the list is empty", async () => {
    vi.stubGlobal("fetch", makeFetchMock({ list: [] }));
    renderView();
    await waitFor(() =>
      expect(screen.getByText("New prompt")).toBeTruthy(),
    );
  });

  it("Save calls PUT /api/prompts/{name} with the edited content", async () => {
    const fetchMock = makeFetchMock({
      list: [
        { name: "main", size_bytes: 200, mtime: "2026-01-02T00:00:00+00:00" },
      ],
      get: {
        main: { name: "main", content: "original body" },
      },
    });
    vi.stubGlobal("fetch", fetchMock);
    renderView();
    await waitFor(() => expect(screen.getByText("main")).toBeTruthy());
    fireEvent.click(screen.getByText("main"));
    const textarea = (await screen.findByRole("textbox")) as HTMLTextAreaElement;
    expect(textarea.value).toBe("original body");
    fireEvent.change(textarea, { target: { value: "edited body" } });
    const saveButton = screen.getByText("Save");
    expect(saveButton).toBeTruthy();
    fireEvent.click(saveButton);
    await waitFor(() => {
      const putCalls = fetchMock.mock.calls.filter(
        (args: unknown[]) => {
          const u = args[0] as string;
          const o = args[1] as RequestInit | undefined;
          return (
            typeof u === "string" &&
            u === "/api/prompts/main" &&
            o?.method === "PUT"
          );
        },
      );
      expect(putCalls.length).toBeGreaterThanOrEqual(1);
      const body = JSON.parse(
        (putCalls[0]![1] as RequestInit).body as string,
      ) as { content: string };
      expect(body.content).toBe("edited body");
    });
  });

  it("New prompt flow calls POST /api/prompts and selects the new prompt", async () => {
    const postedNames: string[] = [];
    const fetchMock = makeFetchMock({
      list: [
        { name: "main", size_bytes: 200, mtime: "2026-01-02T00:00:00+00:00" },
      ],
      get: {
        main: { name: "main", content: "main body" },
        coder: { name: "coder", content: "seeded default" },
      },
      postBody: { name: "coder", content: "seeded default" },
      postedNames,
    });
    vi.stubGlobal("fetch", fetchMock);
    renderView();
    await waitFor(() => expect(screen.getByText("main")).toBeTruthy());
    fireEvent.click(screen.getByText("New prompt"));
    // Dialog opens with a name input.
    const nameInput = await screen.findByPlaceholderText("e.g. coder, office-expert");
    fireEvent.change(nameInput, { target: { value: "coder" } });
    fireEvent.click(screen.getByText("Create"));
    // POST was called with {name: "coder"}.
    await waitFor(() => expect(postedNames).toContain("coder"));
    // The new prompt appears in the list and is selected (its content loads).
    await waitFor(() =>
      expect(
        (screen.getByRole("textbox") as HTMLTextAreaElement).value,
      ).toBe("seeded default"),
    );
  });

  it("409 on duplicate name shows an error in the dialog", async () => {
    const fetchMock = makeFetchMock({
      list: [
        { name: "main", size_bytes: 200, mtime: "2026-01-02T00:00:00+00:00" },
      ],
      postStatus: 409,
    });
    vi.stubGlobal("fetch", fetchMock);
    renderView();
    await waitFor(() => expect(screen.getByText("main")).toBeTruthy());
    fireEvent.click(screen.getByText("New prompt"));
    const nameInput = await screen.findByPlaceholderText("e.g. coder, office-expert");
    fireEvent.change(nameInput, { target: { value: "main" } });
    fireEvent.click(screen.getByText("Create"));
    // The duplicate-name error is shown inline (dialog stays open).
    await waitFor(() =>
      expect(screen.getByText(/already exists/i)).toBeTruthy(),
    );
    // Dialog is still open (Create button still visible).
    expect(screen.getByText("Create")).toBeTruthy();
  });

  it("invalid name shows a validation error without calling POST", async () => {
    const postedNames: string[] = [];
    const fetchMock = makeFetchMock({
      list: [
        { name: "main", size_bytes: 200, mtime: "2026-01-02T00:00:00+00:00" },
      ],
      postedNames,
    });
    vi.stubGlobal("fetch", fetchMock);
    renderView();
    await waitFor(() => expect(screen.getByText("main")).toBeTruthy());
    fireEvent.click(screen.getByText("New prompt"));
    const nameInput = await screen.findByPlaceholderText("e.g. coder, office-expert");
    // Uppercase name fails the regex.
    fireEvent.change(nameInput, { target: { value: "BadName" } });
    fireEvent.click(screen.getByText("Create"));
    await waitFor(() =>
      expect(screen.getByText(/Invalid name/i)).toBeTruthy(),
    );
    // No POST was issued.
    expect(postedNames).toHaveLength(0);
  });

  it("Delete button calls DELETE /api/prompts/{name} after confirm", async () => {
    const deletedNames: string[] = [];
    const fetchMock = makeFetchMock({
      list: [
        { name: "main", size_bytes: 200, mtime: "2026-01-02T00:00:00+00:00" },
      ],
      deletedNames,
    });
    vi.stubGlobal("fetch", fetchMock);
    renderView();
    await waitFor(() => expect(screen.getByText("main")).toBeTruthy());
    // Click the trash button on the prompt row.
    const deleteBtn = screen.getByRole("button", { name: "Delete prompt main" });
    fireEvent.click(deleteBtn);
    // Confirm dialog appears — click Delete to confirm.
    const confirmBtn = await screen.findByText("Delete");
    fireEvent.click(confirmBtn);
    await waitFor(() => expect(deletedNames).toContain("main"));
    // Verify the DELETE call targeted the right URL.
    const deleteCalls = fetchMock.mock.calls.filter(
      (args: unknown[]) => {
        const u = args[0] as string;
        const o = args[1] as RequestInit | undefined;
        return typeof u === "string" && u === "/api/prompts/main" && o?.method === "DELETE";
      },
    );
    expect(deleteCalls.length).toBeGreaterThanOrEqual(1);
  });

  it("409 on delete renders the usage dialog listing pool, kind, and agent", async () => {
    const fetchMock = makeFetchMock({
      list: [
        { name: "shared", size_bytes: 200, mtime: "2026-01-02T00:00:00+00:00" },
      ],
      deleteStatus: 409,
      deleteUsages: [
        { pool: "default", agent_kind: "main", agent_name: "main-agent" },
        { pool: "coder", agent_kind: "subagent", agent_name: "worker" },
      ],
    });
    vi.stubGlobal("fetch", fetchMock);
    renderView();
    await waitFor(() => expect(screen.getByText("shared")).toBeTruthy());
    // Click trash → confirm → 409 renders the in-use dialog.
    fireEvent.click(screen.getByRole("button", { name: "Delete prompt shared" }));
    fireEvent.click(await screen.findByText("Delete"));
    // The in-use dialog shows the prompt name in its title.
    await waitFor(() =>
      expect(screen.getByText(/is in use/i)).toBeTruthy(),
    );
    // Each usage row is rendered: pool names + agent names appear.
    expect(screen.getByText("default")).toBeTruthy();
    expect(screen.getByText("coder")).toBeTruthy();
    expect(screen.getByText("main-agent")).toBeTruthy();
    expect(screen.getByText("worker")).toBeTruthy();
    // The table headers are present.
    expect(screen.getByText("Pool")).toBeTruthy();
    expect(screen.getByText("Kind")).toBeTruthy();
    expect(screen.getByText("Agent")).toBeTruthy();
  });

  it("Successful delete removes the item from the list", async () => {
    const fetchMock = makeFetchMock({
      list: [
        { name: "alpha", size_bytes: 100, mtime: "2026-01-01T00:00:00+00:00" },
        { name: "beta", size_bytes: 200, mtime: "2026-01-02T00:00:00+00:00" },
      ],
    });
    vi.stubGlobal("fetch", fetchMock);
    renderView();
    await waitFor(() => expect(screen.getByText("alpha")).toBeTruthy());
    expect(screen.getByText("beta")).toBeTruthy();
    // Delete alpha.
    fireEvent.click(screen.getByRole("button", { name: "Delete prompt alpha" }));
    fireEvent.click(await screen.findByText("Delete"));
    // alpha is gone; beta remains.
    await waitFor(() => expect(screen.queryByText("alpha")).toBeNull());
    expect(screen.getByText("beta")).toBeTruthy();
  });
});
