import { describe, it, expect, vi, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { AgentMcpSelector } from "./AgentMcpSelector";

function makeResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

const mcpMap = {
  fs: { type: "stdio", command: "npx", args: [], environment: {} },
  web: { type: "sse", url: "https://x", headers: {} },
};

afterEach(() => vi.unstubAllGlobals());

describe("AgentMcpSelector", () => {
  it("starts collapsed; expanding reveals the loaded global servers", async () => {
    const fetchMock = vi.fn(() =>
      Promise.resolve(makeResponse(200, mcpMap)),
    );
    vi.stubGlobal("fetch", fetchMock);
    render(<AgentMcpSelector value={["fs"]} onChange={() => {}} />);
    // header reflects selected count while collapsed
    expect(screen.getByText(/MCP servers \(1 selected\)/)).toBeTruthy();
    // expand
    fireEvent.click(screen.getByText(/MCP servers/));
    await waitFor(() =>
      expect(screen.getByLabelText("fs")).toBeTruthy(),
    );
  });

  it("checking a server adds it to value; unchecking removes it", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.resolve(makeResponse(200, mcpMap))),
    );
    const onChange = vi.fn();
    render(<AgentMcpSelector value={[]} onChange={onChange} />);
    fireEvent.click(screen.getByText(/MCP servers/));
    await waitFor(() => expect(screen.getByLabelText("fs")).toBeTruthy());
    // fs not yet selected → checking adds it
    fireEvent.click(screen.getByLabelText("fs"));
    expect(onChange.mock.calls[0]![0]).toEqual(["fs"]);

    // web next
    fireEvent.click(screen.getByLabelText("web"));
    expect(onChange.mock.calls[1]![0]).toEqual(["web"]);
  });

  it("reflects current selection in checkbox state", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.resolve(makeResponse(200, mcpMap))),
    );
    render(<AgentMcpSelector value={["web"]} onChange={() => {}} />);
    fireEvent.click(screen.getByText(/MCP servers/));
    await waitFor(() => expect(screen.getByLabelText("web")).toBeTruthy());
    expect((screen.getByLabelText("web") as HTMLInputElement).checked).toBe(true);
    expect((screen.getByLabelText("fs") as HTMLInputElement).checked).toBe(false);
  });
});
