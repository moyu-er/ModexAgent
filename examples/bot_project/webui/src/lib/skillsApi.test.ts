import { describe, it, expect, vi, afterEach } from "vitest";
import {
  listSkills,
  uploadSkill,
  deleteSkill,
  listAgentSkills,
  assignSkill,
  unassignSkill,
} from "./skillsApi";

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

describe("skillsApi", () => {
  it("listSkills GETs /api/skills and maps description", async () => {
    const fetchMock = vi.fn(() =>
      Promise.resolve(
        makeResponse(200, [
          { name: "fmt", source: "global", description: "Format code." },
        ]),
      ),
    );
    vi.stubGlobal("fetch", fetchMock);
    const out = await listSkills();
    expect(out[0]!.name).toBe("fmt");
    expect(out[0]!.description).toBe("Format code.");
    const [url, init] = call(fetchMock);
    expect(url).toBe("/api/skills");
    expect(init?.method ?? "GET").toBe("GET");
  });

  it("uploadSkill POSTs {name, files:{relpath:base64}} JSON and maps description", async () => {
    const fetchMock = vi.fn(() =>
      Promise.resolve(
        makeResponse(200, {
          name: "fmt",
          source: "global",
          description: "Format code.",
        }),
      ),
    );
    vi.stubGlobal("fetch", fetchMock);
    const out = await uploadSkill("fmt", [
      { relpath: "SKILL.md", content: "aGVsbG8=" },
      { relpath: "run.sh", content: "IyEvYmluL2Jhc2g=" },
    ]);
    expect(out.description).toBe("Format code.");
    const [url, init] = call(fetchMock);
    expect(url).toBe("/api/skills");
    expect(init?.method).toBe("POST");
    const body = JSON.parse(String(init!.body)) as {
      name: string;
      files: Record<string, string>;
    };
    expect(body.name).toBe("fmt");
    expect(body.files["SKILL.md"]).toBe("aGVsbG8=");
    expect(body.files["run.sh"]).toBe("IyEvYmluL2Jhc2g=");
  });

  it("deleteSkill DELETEs /api/skills/{name}", async () => {
    const fetchMock = vi.fn(() =>
      Promise.resolve(makeResponse(200, { deleted: "fmt" })),
    );
    vi.stubGlobal("fetch", fetchMock);
    await deleteSkill("fmt");
    const [url, init] = call(fetchMock);
    expect(url).toBe("/api/skills/fmt");
    expect(init?.method).toBe("DELETE");
  });

  it("listAgentSkills / assign / unassign hit the per-agent route and maps description", async () => {
    const fetchMock = vi.fn(() =>
      Promise.resolve(
        makeResponse(200, [
          { name: "fmt", source: "global", description: "Format code." },
        ]),
      ),
    );
    vi.stubGlobal("fetch", fetchMock);
    const out = await listAgentSkills("p", "a");
    expect(out[0]!.description).toBe("Format code.");
    await assignSkill("p", "a", "fmt");
    await unassignSkill("p", "a", "fmt");
    expect(call(fetchMock, 0)[0]).toBe("/api/pools/p/agents/a/skills");
    expect(call(fetchMock, 1)[1]?.method).toBe("POST");
    expect(call(fetchMock, 1)[0]).toBe("/api/pools/p/agents/a/skills/fmt");
    expect(call(fetchMock, 2)[1]?.method).toBe("DELETE");
  });
});
