import { describe, it, expect, vi, afterEach } from "vitest";
import {
  ApiError,
  fetchApprovals,
  fetchPools,
  fetchProviderModels,
  submitApproval,
} from "./api";

// Minimal Response factory. The Response constructor derives `ok` from the
// status code, and its body-consumption guard makes json() throw if text() was
// already called — both behaviors we rely on here.
function makeResponse(
  status: number,
  statusText: string,
  body: string,
): Response {
  return new Response(body, { status, statusText });
}

describe("ApiError", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("carries status, statusText, and detail", () => {
    const err = new ApiError(404, "Not Found", "no such pool");
    expect(err).toBeInstanceOf(Error);
    expect(err.name).toBe("ApiError");
    expect(err.status).toBe(404);
    expect(err.statusText).toBe("Not Found");
    expect(err.detail).toBe("no such pool");
    expect(err.message).toContain("404");
    expect(err.message).toContain("no such pool");
  });

  it("a non-2xx response causes a fetch helper to throw ApiError", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() =>
        Promise.resolve(
          makeResponse(500, "Internal Server Error", "boom"),
        ),
      ),
    );

    await expect(fetchPools()).rejects.toThrow(ApiError);
    await expect(fetchPools()).rejects.toMatchObject({
      status: 500,
      statusText: "Internal Server Error",
      detail: "boom",
    });
  });

  it("a 2xx response parses normally without throwing", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() =>
        Promise.resolve(
          makeResponse(200, "OK", JSON.stringify([{ name: "main" }])),
        ),
      ),
    );

    const result = await fetchPools();
    expect(result).toEqual([{ name: "main" }]);
  });
});

describe("approval API", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("fetchApprovals GETs /approvals with ws param and returns views", async () => {
    const fake = [
      {
        tool_call_id: "c1",
        tool_name: "write_file",
        tier: "dangerous",
        arguments: {},
        status: "pending",
      },
    ];
    vi.stubGlobal(
      "fetch",
      vi.fn(() =>
        Promise.resolve(
          makeResponse(200, "OK", JSON.stringify(fake)),
        ),
      ),
    );

    const out = await fetchApprovals("s.main", "ws0");
    expect(out).toEqual(fake);
    expect(fetch).toHaveBeenCalledWith(
      "/api/sessions/s.main/approvals?ws=ws0",
    );
  });

  it("submitApproval POSTs the decision body with tool_call_id and action", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() =>
        Promise.resolve(
          makeResponse(202, "Accepted", JSON.stringify({ accepted: true })),
        ),
      ),
    );

    const out = await submitApproval("s.main", "c1", "allow", "ws0");
    expect(out).toEqual({ accepted: true });
    expect(fetch).toHaveBeenCalledWith(
      "/api/sessions/s.main/approvals?ws=ws0",
      expect.objectContaining({
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ tool_call_id: "c1", action: "allow" }),
      }),
    );
  });
});

describe("fetchProviderModels", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("Form A: POSTs {provider_key} to /api/models/fetch", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() =>
        Promise.resolve(
          makeResponse(200, "OK", JSON.stringify({ models: [{ id: "m1" }] })),
        ),
      ),
    );

    const out = await fetchProviderModels({ provider_key: "deepseek" });
    expect(out).toEqual({ models: [{ id: "m1" }] });
    expect(fetch).toHaveBeenCalledWith(
      "/api/models/fetch",
      expect.objectContaining({
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ provider_key: "deepseek" }),
      }),
    );
  });

  it("Form B: POSTs inline connection info to /api/models/fetch", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() =>
        Promise.resolve(
          makeResponse(200, "OK", JSON.stringify({ models: [{ id: "m1" }] })),
        ),
      ),
    );

    const out = await fetchProviderModels({
      base_url: "https://api.x.com",
      api_key: "sk-test",
      interface_format: "openai_compatible",
      models_url: null,
    });
    expect(out).toEqual({ models: [{ id: "m1" }] });
    expect(fetch).toHaveBeenCalledWith(
      "/api/models/fetch",
      expect.objectContaining({
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          base_url: "https://api.x.com",
          api_key: "sk-test",
          interface_format: "openai_compatible",
          models_url: null,
        }),
      }),
    );
  });

  it("throws ApiError on non-2xx", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() =>
        Promise.resolve(makeResponse(422, "Unprocessable", '{"error":"validation"}')),
      ),
    );

    await expect(
      fetchProviderModels({ provider_key: "x" }),
    ).rejects.toMatchObject({ status: 422 });
  });
});
