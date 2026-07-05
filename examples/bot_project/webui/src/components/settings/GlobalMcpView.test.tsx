import { describe, it, expect, vi, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
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
};

afterEach(() => vi.unstubAllGlobals());

function renderView(): void {
  render(
    <ToastProvider>
      <GlobalMcpView />
    </ToastProvider>,
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
    // transport normalized type→transport → select shows "stdio"
    const transportSelect = screen.getByRole("combobox") as HTMLSelectElement;
    expect(transportSelect.value).toBe("stdio");
  });

  it("Add server creates a new empty card", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.resolve(makeResponse(200, mcpMap))),
    );
    renderView();
    await waitFor(() => expect(screen.getByDisplayValue("npx")).toBeTruthy());
    const before = screen.getAllByRole("combobox").length;
    fireEvent.click(screen.getByText("+ Add server"));
    expect(screen.getAllByRole("combobox").length).toBe(before + 1);
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

  it("delete-in-use shows conflict (409 surfaces used_by)", async () => {
    const fetchMock = vi.fn((url: string, init?: RequestInit) => {
      const method = init?.method ?? "GET";
      if (url === "/api/mcp") return Promise.resolve(makeResponse(200, mcpMap));
      if (url === "/api/mcp/fs" && method === "DELETE") {
        return Promise.resolve(
          makeResponse(409, {
            error: "in use",
            used_by: [["default", "main"]],
          }),
        );
      }
      return Promise.resolve(makeResponse(200, {}));
    });
    vi.stubGlobal("fetch", fetchMock);
    renderView();
    await waitFor(() => expect(screen.getByDisplayValue("npx")).toBeTruthy());
    fireEvent.click(screen.getByRole("button", { name: "Delete server" }));
    // Both the toast and the inline conflict <p> surface the used_by list.
    // Assert at least one match for the specific phrasing.
    await waitFor(() =>
      expect(screen.getAllByText(/In use by default\/main/).length).toBeGreaterThan(0),
    );
  });
});
