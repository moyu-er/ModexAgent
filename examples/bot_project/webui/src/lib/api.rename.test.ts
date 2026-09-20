// renameSessionTitle — PATCH /api/sessions/{id}/title?ws=&pool= (PA-02).
//
// Contract: 400 on invalid input surfaces as ApiError(400); 404 as
// ApiError(404); success resolves {updated: true}. Trimming/single-line/80
// validation is enforced SERVER-side (PA-01); the helper sends the raw title.

import { describe, it, expect, vi, afterEach } from "vitest";
import { renameSessionTitle, ApiError } from "./api";

function makeResponse(status: number, statusText: string, body: string): Response {
  return new Response(body, { status, statusText });
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("renameSessionTitle", () => {
  it("PATCHes the title with ws+pool scope params", async () => {
    const fetchMock = vi.fn(() =>
      Promise.resolve(makeResponse(200, "OK", JSON.stringify({ updated: true }))),
    );
    vi.stubGlobal("fetch", fetchMock);

    await expect(
      renameSessionTitle("abc.main", "Trip plan", "/ws/a", "main"),
    ).resolves.toEqual({ updated: true });

    const call = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(call[0]).toBe("/api/sessions/abc.main/title?ws=%2Fws%2Fa&pool=main");
    expect(call[1].method).toBe("PATCH");
    expect(call[1].headers).toEqual({ "Content-Type": "application/json" });
    expect(call[1].body).toBe(JSON.stringify({ title: "Trip plan" }));
  });

  it("omits scope params for the home workspace and undefined pool", async () => {
    const fetchMock = vi.fn(() =>
      Promise.resolve(makeResponse(200, "OK", JSON.stringify({ updated: true }))),
    );
    vi.stubGlobal("fetch", fetchMock);

    await renameSessionTitle("abc.main", "T", undefined, undefined);
    const call = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(call[0]).toBe("/api/sessions/abc.main/title");
  });

  it("throws ApiError(400) on invalid input (server-validated)", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() =>
        Promise.resolve(makeResponse(400, "Bad Request", "title must be a single line")),
      ),
    );
    await expect(
      renameSessionTitle("abc.main", "two\nlines", undefined, undefined),
    ).rejects.toMatchObject({ status: 400, name: "ApiError" });
    await expect(
      renameSessionTitle("abc.main", "x".repeat(81), undefined, undefined),
    ).rejects.toBeInstanceOf(ApiError);
  });

  it("throws ApiError(404) when the session does not exist", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.resolve(makeResponse(404, "Not Found", "no such session"))),
    );
    await expect(
      renameSessionTitle("gone.main", "T", undefined, undefined),
    ).rejects.toMatchObject({ status: 404 });
  });

  it("treats an identical-title save (no-op success) as success", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() =>
        Promise.resolve(makeResponse(200, "OK", JSON.stringify({ updated: true }))),
      ),
    );
    await expect(
      renameSessionTitle("abc.main", "Same", undefined, undefined),
    ).resolves.toEqual({ updated: true });
  });
});
