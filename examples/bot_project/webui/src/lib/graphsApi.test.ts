import { describe, it, expect, vi, afterEach } from "vitest";
import {
  getSpecs,
  getSpec,
  updateSpec,
  runGraph,
  listInstances,
  getInstance,
  getEvents,
  pauseGraph,
  resumeGraph,
  stopGraph,
  deliverToNode,
} from "./graphsApi";

function makeResponse(status: number, body: unknown): Response {
  return new Response(typeof body === "string" ? body : JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

type FetchArgs = [string, RequestInit?];
function call(mock: { mock: { calls: unknown[] } }, i = 0): FetchArgs {
  return mock.mock.calls[i] as unknown as FetchArgs;
}

function headersOf(init: RequestInit | undefined): Record<string, string> {
  return (init?.headers ?? {}) as Record<string, string>;
}

afterEach(() => vi.unstubAllGlobals());

describe("graphsApi", () => {
  it("getSpecs GETs /api/graphs/specs and unwraps the specs array", async () => {
    const fetchMock = vi.fn(() =>
      Promise.resolve(
        makeResponse(200, { specs: [{ spec_id: 1, name: "g", version: "1" }] }),
      ),
    );
    vi.stubGlobal("fetch", fetchMock);
    const out = await getSpecs("");
    expect(out).toHaveLength(1);
    const [url, init] = call(fetchMock, 0);
    expect(url).toBe("/api/graphs/specs");
    expect(init?.method ?? "GET").toBe("GET");
    // Home workspace ("") omits the header so the server resolves home.
    expect(headersOf(init)["X-Workspace-Id"]).toBeUndefined();
  });

  it("sends X-Workspace-Id for non-home workspaces", async () => {
    const fetchMock = vi.fn(() =>
      Promise.resolve(makeResponse(200, { specs: [] })),
    );
    vi.stubGlobal("fetch", fetchMock);
    await getSpecs("F:/proj");
    const [, init] = call(fetchMock, 0);
    expect(headersOf(init)["X-Workspace-Id"]).toBe("F:/proj");
  });

  it("getSpec GETs /api/graphs/specs/{id}", async () => {
    const fetchMock = vi.fn(() =>
      Promise.resolve(
        makeResponse(200, { spec_id: "3", name: "g", version: "1", yaml_content: "a: b" }),
      ),
    );
    vi.stubGlobal("fetch", fetchMock);
    const out = await getSpec("", "3");
    expect(out.yaml_content).toBe("a: b");
    const [url] = call(fetchMock, 0);
    expect(url).toBe("/api/graphs/specs/3");
  });

  it("updateSpec PUTs {yaml_content} to /api/graphs/specs/{id}", async () => {
    const fetchMock = vi.fn(() =>
      Promise.resolve(
        makeResponse(200, { spec_id: "3", name: "g", version: "2", yaml_content: "a: c" }),
      ),
    );
    vi.stubGlobal("fetch", fetchMock);
    await updateSpec("ws", "3", "a: c");
    const [url, init] = call(fetchMock, 0);
    expect(url).toBe("/api/graphs/specs/3");
    expect(init?.method).toBe("PUT");
    expect(JSON.parse(init?.body as string)).toEqual({ yaml_content: "a: c" });
    expect(headersOf(init)["X-Workspace-Id"]).toBe("ws");
  });

  it("runGraph POSTs user_input as a GraphPayload", async () => {
    const fetchMock = vi.fn(() =>
      Promise.resolve(makeResponse(200, { graph_instance_id: "9", status: "pending" })),
    );
    vi.stubGlobal("fetch", fetchMock);
    const out = await runGraph("", "3", "hello");
    expect(out.graph_instance_id).toBe("9");
    const [url, init] = call(fetchMock, 0);
    expect(url).toBe("/api/graphs/specs/3/run");
    expect(init?.method).toBe("POST");
    expect(JSON.parse(init?.body as string)).toEqual({
      user_input: { content: "hello" },
    });
  });

  it("runGraph without input POSTs an empty object", async () => {
    const fetchMock = vi.fn(() =>
      Promise.resolve(makeResponse(200, { graph_instance_id: "9", status: "pending" })),
    );
    vi.stubGlobal("fetch", fetchMock);
    await runGraph("", "3");
    const [, init] = call(fetchMock, 0);
    expect(JSON.parse(init?.body as string)).toEqual({});
  });

  it("listInstances appends ?status= only when provided", async () => {
    const fetchMock = vi.fn(() => Promise.resolve(makeResponse(200, [])));
    vi.stubGlobal("fetch", fetchMock);
    await listInstances("ws", "running");
    await listInstances("ws");
    const [urlFiltered] = call(fetchMock, 0);
    const [urlAll] = call(fetchMock, 1);
    expect(urlFiltered).toBe("/api/graphs/instances?status=running");
    expect(urlAll).toBe("/api/graphs/instances");
  });

  it("getInstance GETs /api/graphs/instances/{id}", async () => {
    const fetchMock = vi.fn(() =>
      Promise.resolve(
        makeResponse(200, {
          spec_id: "3",
          graph_instance_id: "4",
          status: "running",
          nodes: [{ node_name: "researcher", node_id: "node_ab", status: "running" }],
          result: null,
        }),
      ),
    );
    vi.stubGlobal("fetch", fetchMock);
    const out = await getInstance("", "4");
    expect(out.nodes).toHaveLength(1);
    expect(out.spec_id).toBe("3");
    const [url] = call(fetchMock, 0);
    expect(url).toBe("/api/graphs/instances/4");
  });

  it("getEvents GETs /api/graphs/instances/{id}/events and unwraps events", async () => {
    const fetchMock = vi.fn(() =>
      Promise.resolve(
        makeResponse(200, {
          events: [{ kind: "graph_completed", graph_instance_id: "4", result: null, error: null }],
        }),
      ),
    );
    vi.stubGlobal("fetch", fetchMock);
    const out = await getEvents("", "4");
    expect(out[0]!.kind).toBe("graph_completed");
    const [url] = call(fetchMock, 0);
    expect(url).toBe("/api/graphs/instances/4/events");
  });

  it.each([
    ["pauseGraph", pauseGraph, "pause"],
    ["resumeGraph", resumeGraph, "resume"],
    ["stopGraph", stopGraph, "stop"],
  ] as const)("%s POSTs the %s control action", async (_name, fn, action) => {
    const fetchMock = vi.fn(() =>
      Promise.resolve(makeResponse(200, { graph_instance_id: "4", status: action })),
    );
    vi.stubGlobal("fetch", fetchMock);
    await fn("ws", "4");
    const [url, init] = call(fetchMock, 0);
    expect(url).toBe(`/api/graphs/instances/4/${action}`);
    expect(init?.method).toBe("POST");
    expect(headersOf(init)["X-Workspace-Id"]).toBe("ws");
  });

  it("deliverToNode POSTs {node_name, content: {content}} to /deliver", async () => {
    const fetchMock = vi.fn(() =>
      Promise.resolve(
        makeResponse(200, { graph_instance_id: "4", node_name: "n", status: "delivered" }),
      ),
    );
    vi.stubGlobal("fetch", fetchMock);
    await deliverToNode("ws", "4", "n", "payload");
    const [url, init] = call(fetchMock, 0);
    expect(url).toBe("/api/graphs/instances/4/deliver");
    expect(init?.method).toBe("POST");
    expect(JSON.parse(init?.body as string)).toEqual({
      node_name: "n",
      content: { content: "payload" },
    });
  });

  it("throws ApiError with the backend detail on non-2xx", async () => {
    const fetchMock = vi.fn(() =>
      Promise.resolve(makeResponse(400, { error: "invalid spec", detail: "bad yaml" })),
    );
    vi.stubGlobal("fetch", fetchMock);
    await expect(updateSpec("", "3", "x")).rejects.toThrow(/400/);
  });
});
