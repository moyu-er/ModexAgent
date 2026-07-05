import { describe, it, expect, vi, afterEach } from "vitest";
import { fetchConfig, saveConfig, restartSystem } from "./api";

function makeResponse(status: number, statusText: string, body: string): Response {
  return new Response(body, { status, statusText });
}

afterEach(() => vi.unstubAllGlobals());

describe("config api", () => {
  it("fetchConfig GETs /api/config/{domain}", async () => {
    vi.stubGlobal("fetch", vi.fn(() =>
      Promise.resolve(makeResponse(200, "OK", JSON.stringify({ domain: "im", flavor: "registry", restart_required: false })))));
    const out = await fetchConfig("im");
    expect(out.domain).toBe("im");
    expect(fetch).toHaveBeenCalledWith("/api/config/im");
  });

  it("saveConfig PUTs JSON to /api/config/{domain}", async () => {
    vi.stubGlobal("fetch", vi.fn(() =>
      Promise.resolve(makeResponse(200, "OK", JSON.stringify({ restart_required: true })))));
    const out = await saveConfig("im", { telegram: { token: { value: "x" } } });
    expect(out.restart_required).toBe(true);
    expect(fetch).toHaveBeenCalledWith(
      "/api/config/im",
      expect.objectContaining({ method: "PUT", body: JSON.stringify({ telegram: { token: { value: "x" } } }) }),
    );
  });

  it("restartSystem POSTs /api/system/restart", async () => {
    vi.stubGlobal("fetch", vi.fn(() =>
      Promise.resolve(makeResponse(200, "OK", JSON.stringify({ restarting: true })))));
    const out = await restartSystem();
    expect(out.restarting).toBe(true);
    expect(fetch).toHaveBeenCalledWith("/api/system/restart", expect.objectContaining({ method: "POST" }));
  });
});
