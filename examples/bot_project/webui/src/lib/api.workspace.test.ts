// cdWorkspace: the workspace-tab "open recent" seam — cd + cwd coercion.

import { describe, it, expect, vi, afterEach } from "vitest";
import { cdWorkspace, createWorkspace } from "./api";

function stubCdResponse(body: unknown, ok = true): void {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue({
      ok,
      status: ok ? 200 : 500,
      statusText: ok ? "OK" : "Server Error",
      json: () => Promise.resolve(body),
      text: () => Promise.resolve(JSON.stringify(body)),
    }),
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("cdWorkspace", () => {
  it("returns the string cwd on success", async () => {
    stubCdResponse({ success: true, cwd: "/ws_a", notice: "" });
    await expect(cdWorkspace("/ws_a")).resolves.toBe("/ws_a");
  });

  it("coerces a non-string cwd (backend path-object quirk) to a string", async () => {
    stubCdResponse({
      success: true,
      cwd: { toString: () => "/ws_obj" },
      notice: "",
    });
    await expect(cdWorkspace("/ws_obj")).resolves.toBe("/ws_obj");
  });

  it("throws the backend notice when the cd is rejected", async () => {
    stubCdResponse({ success: false, cwd: "", notice: "directory does not exist" });
    await expect(cdWorkspace("/gone")).rejects.toThrow("directory does not exist");
  });
});

describe("createWorkspace", () => {
  it("POSTs name+backend and returns the new workspace path", async () => {
    stubCdResponse({
      success: true,
      name: "alpha",
      path: "/proj/subworkspace/alpha",
      notice: "created",
    });
    await expect(createWorkspace("alpha", "sqlite")).resolves.toBe(
      "/proj/subworkspace/alpha",
    );
    const fetchMock = vi.mocked(fetch);
    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining("/api/workspace/create"),
      expect.objectContaining({ method: "POST" }),
    );
    const init = fetchMock.mock.calls[0]?.[1] as RequestInit;
    expect(JSON.parse(String(init.body))).toEqual({ name: "alpha", backend: "sqlite" });
  });

  it("throws the backend error message on rejection", async () => {
    stubCdResponse({ error: "a workspace named 'alpha' already exists" }, false);
    await expect(createWorkspace("alpha", null)).rejects.toThrow("already exists");
  });
});
