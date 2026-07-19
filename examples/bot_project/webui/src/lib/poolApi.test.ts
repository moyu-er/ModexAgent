import { describe, it, expect, vi, afterEach } from "vitest";
import {
  listPools,
  getPool,
  savePool,
  createPool,
  deletePool,
  addPeer,
  removePeer,
} from "./poolApi";
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
          { name: "default", main_agent_name: "main", subagent_count: 2 },
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

  it("savePool PUTs the tree to /api/pools/{name}", async () => {
    const fetchMock = vi.fn(() =>
      Promise.resolve(makeResponse(200, { name: "p", written: true })),
    );
    vi.stubGlobal("fetch", fetchMock);
    const tree = {
      name: "p",
      main_agent_name: "main",
      main: { agent_name: "main" },
      subagents: [],
      restart_required: false,
    };
    await savePool("p", tree as never);
    const [url, init] = call(fetchMock, 0);
    expect(url).toBe("/api/pools/p");
    expect(init?.method).toBe("PUT");
    expect(init?.body).toBe(JSON.stringify(tree));
  });

  it("createPool POSTs {name}", async () => {
    const fetchMock = vi.fn(() =>
      Promise.resolve(makeResponse(200, { name: "p" })),
    );
    vi.stubGlobal("fetch", fetchMock);
    await createPool("p");
    const [url, init] = call(fetchMock, 0);
    expect(url).toBe("/api/pools");
    expect(init?.method).toBe("POST");
    expect(init?.body).toBe(JSON.stringify({ name: "p" }));
  });

  it("addPeer POSTs {peer} to /api/pools/{pool}/peers", async () => {
    const fetchMock = vi.fn(() =>
      Promise.resolve(makeResponse(200, { pool_a: { name: "a" }, pool_b: { name: "b" } })),
    );
    vi.stubGlobal("fetch", fetchMock);
    const result = await addPeer("a", "b");
    const [url, init] = call(fetchMock, 0);
    expect(url).toBe("/api/pools/a/peers");
    expect(init?.method).toBe("POST");
    expect(init?.body).toBe(JSON.stringify({ peer: "b" }));
    expect(result.pool_a).toEqual({ name: "a" });
    expect(result.pool_b).toEqual({ name: "b" });
  });

  it("removePeer DELETEs /api/pools/{pool}/peers/{peer}", async () => {
    const fetchMock = vi.fn(() =>
      Promise.resolve(makeResponse(200, { pool_a: { name: "a" }, pool_b: { name: "b" } })),
    );
    vi.stubGlobal("fetch", fetchMock);
    const result = await removePeer("a", "b");
    const [url, init] = call(fetchMock, 0);
    expect(url).toBe("/api/pools/a/peers/b");
    expect(init?.method).toBe("DELETE");
    expect(result.pool_a).toEqual({ name: "a" });
    expect(result.pool_b).toEqual({ name: "b" });
  });

  it("deletePool DELETEs /api/pools/{name}", async () => {
    const fetchMock = vi.fn(() =>
      Promise.resolve(makeResponse(200, { deleted: "p" })),
    );
    vi.stubGlobal("fetch", fetchMock);
    await deletePool("p");
    const [url, init] = call(fetchMock, 0);
    expect(url).toBe("/api/pools/p");
    expect(init?.method).toBe("DELETE");
  });

  it("throws ApiError carrying status + detail on non-2xx", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() =>
        Promise.resolve(makeResponse(404, { error: "unknown pool: x" })),
      ),
    );
    await expect(getPool("x")).rejects.toMatchObject({
      name: "ApiError",
      status: 404,
    });
    await expect(getPool("x")).rejects.toBeInstanceOf(ApiError);
  });
});
