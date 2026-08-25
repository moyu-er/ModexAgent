import { describe, it, expect, vi, afterEach } from "vitest";
import { listPools } from "./poolApi";
import { ApiError } from "./api";

function makeResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

type FetchArgs = [string, RequestInit?];
function call(mock: { mock: { calls: unknown[] } }, i = 0): FetchArgs {
  return mock.mock.calls[i] as unknown as FetchArgs;
}

afterEach(() => vi.unstubAllGlobals());

describe("poolApi", () => {
  it("listPools GETs /api/pools and parses summaries", async () => {
    const fetchMock = vi.fn(() =>
      Promise.resolve(
        makeResponse(200, [
          { name: "default", root_agent_name: "default", subagent_count: 2 },
        ]),
      ),
    );
    vi.stubGlobal("fetch", fetchMock);
    const out = await listPools();
    expect(out).toHaveLength(1);
    expect(out[0]!.name).toBe("default");
    const [url, init] = call(fetchMock, 0);
    expect(url).toBe("/api/pools");
    expect(init?.method ?? "GET").toBe("GET");
  });

  it("throws ApiError carrying status + detail on non-2xx", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() =>
        Promise.resolve(makeResponse(500, { error: "read failed" })),
      ),
    );
    await expect(listPools()).rejects.toMatchObject({
      name: "ApiError",
      status: 500,
    });
    await expect(listPools()).rejects.toBeInstanceOf(ApiError);
  });
});
