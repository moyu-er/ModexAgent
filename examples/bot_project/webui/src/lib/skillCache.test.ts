import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  clearSkillCache,
  getPoolSkills,
  hasPoolSkills,
  peekPoolSkills,
  refreshPoolSkills,
} from "./skillCache";
import * as skillsApi from "../lib/skillsApi";
import type { SkillEntry } from "../types/pool";

vi.mock("../lib/skillsApi", () => ({ listAgentSkills: vi.fn() }));
const mockList = vi.mocked(skillsApi.listAgentSkills);

function skill(name: string, description?: string): SkillEntry {
  return { name, source: "local", description };
}

describe("skillCache", () => {
  beforeEach(() => {
    clearSkillCache();
    mockList.mockReset();
  });

  afterEach(() => {
    clearSkillCache();
  });

  it("returns [] for empty pool or mainAgent without calling the API", async () => {
    await expect(getPoolSkills("", "main")).resolves.toEqual([]);
    await expect(getPoolSkills("default", "")).resolves.toEqual([]);
    expect(mockList).not.toHaveBeenCalled();
    expect(hasPoolSkills("", "main")).toBe(false);
    expect(hasPoolSkills("default", "")).toBe(false);
  });

  it("fetches on first access and caches the result (hit on second call, no refetch)", async () => {
    const data = [skill("weather"), skill("github")];
    mockList.mockResolvedValue(data);

    const first = await getPoolSkills("default", "main");
    expect(first).toEqual(data);
    expect(mockList).toHaveBeenCalledTimes(1);

    const second = await getPoolSkills("default", "main");
    expect(second).toEqual(data);
    expect(mockList).toHaveBeenCalledTimes(1); // cached, no new fetch
    expect(hasPoolSkills("default", "main")).toBe(true);
    expect(peekPoolSkills("default", "main")).toEqual(data);
  });

  it("caches independently per (pool, mainAgent) pair", async () => {
    mockList.mockResolvedValueOnce([skill("a")]);
    mockList.mockResolvedValueOnce([skill("b")]);

    const a = await getPoolSkills("default", "main");
    const b = await getPoolSkills("coder", "orchestrator");
    expect(a.map((s) => s.name)).toEqual(["a"]);
    expect(b.map((s) => s.name)).toEqual(["b"]);
    expect(mockList).toHaveBeenCalledTimes(2);
    // Re-read default:main — still cached, no new call.
    await getPoolSkills("default", "main");
    expect(mockList).toHaveBeenCalledTimes(2);
  });

  it("refreshPoolSkills forces a re-fetch and updates the cache", async () => {
    mockList.mockResolvedValueOnce([skill("v1")]);
    await getPoolSkills("default", "main");

    mockList.mockResolvedValueOnce([skill("v1"), skill("v2")]);
    const refreshed = await refreshPoolSkills("default", "main");
    expect(refreshed.map((s) => s.name)).toEqual(["v1", "v2"]);
    expect(mockList).toHaveBeenCalledTimes(2);
    expect(peekPoolSkills("default", "main")?.map((s) => s.name)).toEqual(["v1", "v2"]);
  });

  it("dedupes concurrent fetches for the same pair into one API call", async () => {
    const data = [skill("weather")];
    mockList.mockResolvedValue(data);

    const [a, b, c] = await Promise.all([
      getPoolSkills("default", "main"),
      getPoolSkills("default", "main"),
      getPoolSkills("default", "main"),
    ]);
    expect(a).toEqual(data);
    expect(b).toEqual(data);
    expect(c).toEqual(data);
    expect(mockList).toHaveBeenCalledTimes(1);
  });

  it("preserves the prior cached value on fetch failure (stale > empty)", async () => {
    mockList.mockResolvedValueOnce([skill("good")]);
    await getPoolSkills("default", "main");

    mockList.mockRejectedValueOnce(new Error("network down"));
    const result = await refreshPoolSkills("default", "main");
    expect(result.map((s) => s.name)).toEqual(["good"]);
    expect(peekPoolSkills("default", "main")?.map((s) => s.name)).toEqual(["good"]);
  });
});
