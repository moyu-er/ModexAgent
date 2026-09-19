// preferencesApi — the personal-assistant preference seam (PA-06 frontend
// client). GET/PUT /api/config/personal_assistant through the usual
// ConfigPayload shape; `values.default_workspace` (null | canonical server
// path) and `values.default_pool` (non-empty string) are the only fields.

import { describe, it, expect, vi, afterEach } from "vitest";
import { fetchPreferences, savePreferences } from "./preferencesApi";

function makeResponse(status: number, body: string): Response {
  return new Response(body, { status });
}

const PAYLOAD = {
  domain: "personal_assistant",
  label: "Personal Assistant",
  flavor: "singleton" as const,
  restart_required: false,
  values: { default_workspace: null, default_pool: "default" },
};

afterEach(() => vi.unstubAllGlobals());

describe("preferencesApi", () => {
  it("fetchPreferences GETs /api/config/personal_assistant and reads values", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.resolve(makeResponse(200, JSON.stringify(PAYLOAD)))),
    );
    const prefs = await fetchPreferences();
    expect(prefs.defaultWorkspace).toBeNull();
    expect(prefs.defaultPool).toBe("default");
    expect(fetch).toHaveBeenCalledWith("/api/config/personal_assistant");
  });

  it("savePreferences PUTs the values map and returns the saved choice", async () => {
    const fetchMock = vi.fn(() =>
      Promise.resolve(
        makeResponse(
          200,
          JSON.stringify({
            ...PAYLOAD,
            values: { default_workspace: "/ws/a", default_pool: "coder" },
          }),
        ),
      ),
    );
    vi.stubGlobal("fetch", fetchMock);
    const saved = await savePreferences({
      defaultWorkspace: "/ws/a",
      defaultPool: "coder",
    });
    expect(saved.defaultWorkspace).toBe("/ws/a");
    expect(saved.defaultPool).toBe("coder");
    type Call = [unknown, RequestInit?];
    const [url, init] = (fetchMock.mock.calls as unknown as Call[])[0]!;
    expect(url).toBe("/api/config/personal_assistant");
    expect(init?.method).toBe("PUT");
    expect(JSON.parse(String(init?.body))).toEqual({
      default_workspace: "/ws/a",
      default_pool: "coder",
    });
  });

  it("rejects on a non-2xx response without inventing values", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.resolve(makeResponse(500, "oops"))),
    );
    await expect(fetchPreferences()).rejects.toThrow();
    await expect(
      savePreferences({ defaultWorkspace: null, defaultPool: "x" }),
    ).rejects.toThrow();
  });
});
