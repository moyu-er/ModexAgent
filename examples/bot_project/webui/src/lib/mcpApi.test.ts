import { describe, it, expect, vi, afterEach } from "vitest";
import { getMcp, upsertMcp, deleteMcp, McpInUseError } from "./mcpApi";
import type { McpServerEntry } from "../types/pool";

function makeResponse(
  status: number,
  body: unknown,
  asText = false,
): Response {
  const text = typeof body === "string" ? body : JSON.stringify(body);
  return new Response(asText ? String(body) : text, {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

type FetchArgs = [string, RequestInit?];
function call(mock: { mock: { calls: unknown[] } }, i = 0): FetchArgs {
  return mock.mock.calls[i] as unknown as FetchArgs;
}

afterEach(() => vi.unstubAllGlobals());

describe("mcpApi", () => {
  it("getMcp GETs /api/mcp and normalizes type→transport / env→env", async () => {
    const fetchMock = vi.fn(() =>
      Promise.resolve(
        // Backend (framework MCPServerEntry) serializes transport as `type`
        // (alias) and env as the field name `env`.
        makeResponse(200, {
          fs: { type: "stdio", command: "npx", env: { FOO: "1" } },
        }),
      ),
    );
    vi.stubGlobal("fetch", fetchMock);
    const out = await getMcp();
    expect(out.fs?.command).toBe("npx");
    expect(out.fs?.transport).toBe("stdio");
    expect(out.fs?.env).toEqual({ FOO: "1" });
    const [url, init] = call(fetchMock, 0);
    expect(url).toBe("/api/mcp");
    expect(init?.method ?? "GET").toBe("GET");
  });

  it("getMcp also accepts the legacy environment alias for env", async () => {
    // The framework model accepts `environment` on input; the normalizer is
    // defensive and accepts it on the wire too.
    vi.stubGlobal(
      "fetch",
      vi.fn(() =>
        Promise.resolve(
          makeResponse(200, { fs: { type: "stdio", environment: { K: "v" } } }),
        ),
      ),
    );
    const out = await getMcp();
    expect(out.fs?.env).toEqual({ K: "v" });
  });

  it("upsertMcp renames transport→type on the wire and normalizes the response", async () => {
    // Backend returns the single persisted entry with by_alias=True wire names
    // (type for transport, env as the field name). upsertMcp must normalize.
    const fetchMock = vi.fn(() =>
      Promise.resolve(
        makeResponse(200, {
          type: "stdio",
          command: "npx",
          args: ["-y", "fs"],
          env: { FOO: "1" },
        }),
      ),
    );
    vi.stubGlobal("fetch", fetchMock);
    const entry: McpServerEntry = {
      transport: "stdio",
      command: "npx",
      args: ["-y", "fs"],
    };
    const saved = await upsertMcp("fs", entry);
    // Return type is a single normalized entry (transport/env, not type/environment).
    expect(saved.transport).toBe("stdio");
    expect(saved.env).toEqual({ FOO: "1" });
    expect((saved as Record<string, unknown>).type).toBeUndefined();
    expect((saved as Record<string, unknown>).environment).toBeUndefined();
    const [url, init] = call(fetchMock, 0);
    expect(url).toBe("/api/mcp/fs");
    expect(init?.method).toBe("PUT");
    const body = JSON.parse(String(init!.body)) as Record<string, unknown>;
    expect(body.type).toBe("stdio");
    expect(body.transport).toBeUndefined();
    expect(body.command).toBe("npx");
  });

  it("deleteMcp DELETEs /api/mcp/{name}", async () => {
    const fetchMock = vi.fn(() =>
      Promise.resolve(makeResponse(200, { deleted: "fs" })),
    );
    vi.stubGlobal("fetch", fetchMock);
    await deleteMcp("fs");
    const [url, init] = call(fetchMock, 0);
    expect(url).toBe("/api/mcp/fs");
    expect(init?.method).toBe("DELETE");
  });

  it("deleteMcp throws McpInUseError carrying used_by on 409", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() =>
        Promise.resolve(
          makeResponse(409, {
            error: "in use",
            used_by: [
              ["default", "main"],
              ["other", "helper"],
            ],
          }),
        ),
      ),
    );
    await expect(deleteMcp("fs")).rejects.toMatchObject({ name: "McpInUseError" });
    try {
      await deleteMcp("fs");
    } catch (e) {
      const err = e as McpInUseError;
      expect(err.usedBy).toEqual([
        ["default", "main"],
        ["other", "helper"],
      ]);
      expect(err.status).toBe(409);
    }
  });
});
