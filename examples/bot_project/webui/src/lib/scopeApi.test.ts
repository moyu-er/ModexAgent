// scopeApi.test.ts — REST client tests: every endpoint appends the active
// workspace via the shared `?ws=` convention (PA-10 workspace consistency),
// and omitting `ws` keeps the historical home-workspace URLs byte-for-byte.

import { describe, it, expect, vi, afterEach } from "vitest";
import {
  getScopeBill,
  getScopeDeclaration,
  getScopeModel,
  getScopeOptions,
  getScopeTopology,
  previewScopeModel,
  saveScopeDeclaration,
  saveScopeModel,
} from "./scopeApi";
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

describe("scopeApi workspace targeting", () => {
  it("GET endpoints append ?ws= when a workspace is given", async () => {
    const fetchMock = vi.fn(() =>
      Promise.resolve(makeResponse(200, { agents: [] })),
    );
    vi.stubGlobal("fetch", fetchMock);
    await getScopeBill("F:/proj/other");
    const [url] = call(fetchMock);
    expect(url).toBe(`/api/scope/bill?ws=${encodeURIComponent("F:/proj/other")}`);
  });

  it("omitting ws keeps the historical home URLs (no query param)", async () => {
    const fetchMock = vi.fn(() =>
      Promise.resolve(makeResponse(200, { agents: [] })),
    );
    vi.stubGlobal("fetch", fetchMock);
    await getScopeBill();
    expect(call(fetchMock)[0]).toBe("/api/scope/bill");
    await getScopeModel(undefined);
    expect(call(fetchMock, 1)[0]).toBe("/api/scope/model");
    await getScopeOptions("");
    expect(call(fetchMock, 2)[0]).toBe("/api/scope/options");
  });

  it("saveScopeModel PUTs the model with the same ws it will be saved under", async () => {
    const fetchMock = vi.fn(() =>
      Promise.resolve(makeResponse(200, { saved: true, restart_required: true })),
    );
    vi.stubGlobal("fetch", fetchMock);
    const model = { pool: { name: "solo", agents: { solo: {} } } };
    const saved = await saveScopeModel(model, "F:/proj/other");
    expect(saved.saved).toBe(true);
    const [url, init] = call(fetchMock);
    expect(url).toBe(`/api/scope/model?ws=${encodeURIComponent("F:/proj/other")}`);
    expect(init?.method).toBe("PUT");
    expect(JSON.parse(init?.body as string)).toEqual({ model });
  });

  it("previewScopeModel POSTs the draft to the requested workspace", async () => {
    const fetchMock = vi.fn(() => Promise.resolve(makeResponse(200, { agents: [] })));
    vi.stubGlobal("fetch", fetchMock);
    await previewScopeModel({ pool: { name: "solo", agents: {} } }, "ws2");
    const [url, init] = call(fetchMock);
    expect(url).toBe("/api/scope/preview?ws=ws2");
    expect(init?.method).toBe("POST");
  });

  it("declaration GET/PUT and topology carry ws the same way", async () => {
    const fetchMock = vi.fn(() =>
      Promise.resolve(makeResponse(200, { yaml: "pool: {}" })),
    );
    vi.stubGlobal("fetch", fetchMock);
    await getScopeDeclaration("ws2");
    expect(call(fetchMock)[0]).toBe("/api/scope/declaration?ws=ws2");
    await saveScopeDeclaration("pool: {}", "ws2");
    const [url, init] = call(fetchMock, 1);
    expect(url).toBe("/api/scope/declaration?ws=ws2");
    expect(init?.method).toBe("PUT");
    await getScopeTopology("ws2");
    expect(call(fetchMock, 2)[0]).toBe("/api/scope/topology?ws=ws2");
  });

  it("surfaces the typed effective faces from the bill payload", async () => {
    const agent = {
      pool: "main",
      agent: "main",
      root: true,
      external: false,
      fields: [],
      tools: [],
      tool_groups: [],
      hooks: [],
      capabilities: [],
      memory: {
        memory_preset: "archive_core",
        archive_enabled: true,
        core_enabled: false,
      },
      approval: { enabled: true, eligible: true },
    };
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.resolve(makeResponse(200, { agents: [agent] }))),
    );
    const [bill] = await getScopeBill("ws2");
    if (!bill) throw new Error("bill agent missing");
    expect(bill.memory.archive_enabled).toBe(true);
    expect(bill.memory.memory_preset).toBe("archive_core");
    expect(bill.approval).toEqual({ enabled: true, eligible: true });
    expect(bill.external).toBe(false);
  });

  it("throws ApiError carrying status + detail on non-2xx", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.resolve(makeResponse(400, { error: "declaration invalid" }))),
    );
    await expect(previewScopeModel({}, "ws2")).rejects.toBeInstanceOf(ApiError);
  });
});
