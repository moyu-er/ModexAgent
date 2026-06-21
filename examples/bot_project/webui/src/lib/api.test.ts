import { describe, it, expect, vi, afterEach } from "vitest";
import { ApiError, fetchPools } from "./api";

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
