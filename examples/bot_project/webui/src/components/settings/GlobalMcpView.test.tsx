import { describe, it, expect, vi, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor, within } from "@testing-library/react";
import { GlobalMcpView } from "./GlobalMcpView";
import { ToastProvider } from "../ToastContext";

function makeResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

// Backend serializes MCP entries with by_alias=True → wire uses type/environment.
const mcpMap = {
  fs: {
    type: "stdio",
    command: "npx",
    args: ["-y", "@mcp/fs"],
    environment: { FOO: "1" },
    cwd: "",
    url: "",
    headers: {},
    timeout: 30,
  },
  // A second persisted card so we can verify only the FIRST is expanded by
  // default and others remain collapsed.
  git: {
    type: "stdio",
    command: "uvx",
    args: [],
    environment: {},
    cwd: "",
    url: "",
    headers: {},
    timeout: 30,
  },
};

afterEach(() => vi.unstubAllGlobals());

function renderView(): void {
  render(
    <ToastProvider>
      <GlobalMcpView />
    </ToastProvider>,
  );
}

/** All expanded card bodies (`id="mcp-card-N-body"`). */
function expandedBodies(): HTMLElement[] {
  return Array.from(
    document.querySelectorAll<HTMLElement>('[id^="mcp-card-"][id$="-body"]'),
  );
}

describe("GlobalMcpView", () => {
  it("renders servers from getMcp (normalized)", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.resolve(makeResponse(200, mcpMap))),
    );
    renderView();
    await waitFor(() =>
      expect(screen.getByDisplayValue("npx")).toBeTruthy(),
    );
    // transport normalized type→transport → the "stdio" category
    // button is the active one for a stdio entry.
    const localBtn = screen.getByRole("button", { name: /^stdio/ });
    expect(localBtn.getAttribute("aria-pressed")).toBe("true");
  });

  it("Add server creates a new empty card", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.resolve(makeResponse(200, mcpMap))),
    );
    renderView();
    await waitFor(() => expect(screen.getByDisplayValue("npx")).toBeTruthy());
    // Count expanded cards via their Name input (only expanded bodies render).
    const before = screen.getAllByLabelText(/^Name/).length;
    fireEvent.click(screen.getByText("Add server"));
    expect(screen.getAllByLabelText(/^Name/).length).toBe(before + 1);
  });

  it("addCard inserts the new card at the top of the list", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.resolve(makeResponse(200, mcpMap))),
    );
    renderView();
    await waitFor(() => expect(screen.getByDisplayValue("npx")).toBeTruthy());

    fireEvent.click(screen.getByText("Add server"));

    // The newly added card is auto-expanded and shows a "New server" header.
    // The first body (top-most card) must be the new one — the empty Name
    // input sits above the persisted "fs" card.
    const bodies = expandedBodies();
    expect(bodies.length).toBe(2);
    const topBody = bodies[0]!;
    const nameInput = within(topBody).getByLabelText(/^Name/) as HTMLInputElement;
    expect(nameInput.value).toBe("");
    // The persisted "fs" command input is still rendered (it's in the second
    // card body, which stays expanded because addCard toggles the new id in
    // without removing the original first-card id — but that doesn't matter
    // for ordering; the new card is structurally first in the DOM).
    expect(screen.getByDisplayValue("npx")).toBeTruthy();
  });

  it("newly added cards auto-expand", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.resolve(makeResponse(200, mcpMap))),
    );
    renderView();
    await waitFor(() => expect(screen.getByDisplayValue("npx")).toBeTruthy());
    // Both persisted cards exist; only the first ("fs") is expanded by default.
    expect(expandedBodies().length).toBe(1);

    fireEvent.click(screen.getByText("Add server"));
    await waitFor(() => {
      expect(expandedBodies().length).toBe(2);
    });
  });

  it("only the first persisted card is expanded by default", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.resolve(makeResponse(200, mcpMap))),
    );
    renderView();
    await waitFor(() => expect(screen.getByDisplayValue("npx")).toBeTruthy());
    const bodies = expandedBodies();
    expect(bodies.length).toBe(1);
    // The expanded body's Command input is the first card's "npx".
    expect(within(bodies[0]!).getByDisplayValue("npx")).toBeTruthy();
    // The second ("git") card is collapsed — its body and "uvx" input are not
    // rendered.
    expect(screen.queryByDisplayValue("uvx")).toBeNull();
  });

  it("edit + Save calls PUT /api/mcp/{name}", async () => {
    const fetchMock = vi.fn((url: string, init?: RequestInit) => {
      if (url === "/api/mcp" && (!init || init.method === undefined)) {
        return Promise.resolve(makeResponse(200, mcpMap));
      }
      // upsert response
      return Promise.resolve(makeResponse(200, { ok: true }));
    });
    vi.stubGlobal("fetch", fetchMock);
    renderView();
    await waitFor(() => expect(screen.getByDisplayValue("npx")).toBeTruthy());
    // edit the command field
    fireEvent.change(screen.getByDisplayValue("npx"), {
      target: { value: "node" },
    });
    // click the Save button on that card
    fireEvent.click(screen.getAllByText("Save")[0]!);
    await waitFor(() => {
      const puts = fetchMock.mock.calls.filter(
        (c) => (c[1]?.method ?? "GET") === "PUT",
      );
      expect(puts.length).toBeGreaterThanOrEqual(1);
    });
  });

  it("delete succeeds even when the server is referenced by agents", async () => {
    const fetchMock = vi.fn((url: string, init?: RequestInit) => {
      const method = init?.method ?? "GET";
      if (url === "/api/mcp") return Promise.resolve(makeResponse(200, mcpMap));
      if (url === "/api/mcp/fs" && method === "DELETE") {
        return Promise.resolve(makeResponse(200, { deleted: "fs" }));
      }
      return Promise.resolve(makeResponse(200, {}));
    });
    vi.stubGlobal("fetch", fetchMock);
    renderView();
    await waitFor(() => expect(screen.getByDisplayValue("npx")).toBeTruthy());
    // Two-step delete: trash reveals a confirm row, then Delete commits.
    fireEvent.click(screen.getAllByRole("button", { name: "Delete server" })[0]!);
    fireEvent.click(screen.getAllByText("Delete")[0]!);
    await waitFor(() => expect(screen.queryByDisplayValue("npx")).toBeNull());
  });
});