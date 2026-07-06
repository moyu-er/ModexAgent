import { describe, it, expect, vi, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { AgentSkillSelector } from "./AgentSkillSelector";
import { ToastProvider } from "../ToastContext";

function makeResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

afterEach(() => vi.unstubAllGlobals());

async function renderSelector(props: { pool?: string; agent?: string } = {}): Promise<void> {
  render(
    <ToastProvider>
      <AgentSkillSelector pool={props.pool ?? "default"} agent={props.agent ?? "main"} />
    </ToastProvider>,
  );
  await waitFor(() =>
    expect(screen.queryByText("Loading…")).toBeNull(),
  );
}

describe("AgentSkillSelector", () => {
  it("lists global + local skills; checked state comes from disk (agent listing)", async () => {
    const fetchMock = vi.fn((url: string) => {
      // listSkills (global registry)
      if (url.endsWith("/api/skills")) {
        return Promise.resolve(
          makeResponse(200, [{ name: "greet", source: "global" }]),
        );
      }
      // listAgentSkills — disk: greet is assigned (and global), scratchpad is local
      if (url.includes("/skills")) {
        return Promise.resolve(
          makeResponse(200, [
            { name: "greet", source: "global" },
            { name: "scratchpad", source: "local" },
          ]),
        );
      }
      return Promise.resolve(makeResponse(200, []));
    });
    vi.stubGlobal("fetch", fetchMock);
    await renderSelector();
    fireEvent.click(screen.getByText(/Skills/));
    await waitFor(() => expect(screen.getByText("greet")).toBeTruthy());
    // greet is assigned on disk → checked; scratchpad is local → no checkbox
    expect((screen.getByLabelText("greet") as HTMLInputElement).checked).toBe(true);
    expect(screen.queryByLabelText("scratchpad")).toBeNull();
    expect(screen.getByText("scratchpad")).toBeTruthy();
    expect(screen.getAllByText(/local/).length).toBeGreaterThan(0);
  });

  it("toggling an assigned global skill calls unassign (DELETE) then re-reads disk", async () => {
    // Mutable disk state: greet is assigned initially, removed after DELETE.
    const disk = new Set<string>(["greet"]);
    const fetchMock = vi.fn((url: string, init?: RequestInit) => {
      const method = init?.method ?? "GET";
      if (url.endsWith("/api/skills")) {
        return Promise.resolve(
          makeResponse(200, [{ name: "greet", source: "global" }]),
        );
      }
      if (url.includes("/skills") && method === "DELETE") {
        disk.delete("greet");
        return Promise.resolve(makeResponse(200, { unassigned: "greet" }));
      }
      if (url.includes("/skills")) {
        return Promise.resolve(
          makeResponse(200, [...disk].map((name) => ({ name, source: "global" }))),
        );
      }
      return Promise.resolve(makeResponse(200, []));
    });
    vi.stubGlobal("fetch", fetchMock);
    await renderSelector();
    fireEvent.click(screen.getByText(/Skills/));
    await waitFor(() =>
      expect((screen.getByLabelText("greet") as HTMLInputElement).checked).toBe(true),
    );
    fireEvent.click(screen.getByLabelText("greet"));
    // checkbox reflects the re-read disk state (greet gone → unchecked)
    await waitFor(() =>
      expect((screen.getByLabelText("greet") as HTMLInputElement).checked).toBe(false),
    );
    // restart toast surfaces
    await waitFor(() =>
      expect(screen.getByText("Saved. Restart to apply.")).toBeTruthy(),
    );
  });

  it("toggling an unassigned global skill calls assign (POST) then re-reads disk", async () => {
    const disk = new Set<string>();
    const fetchMock = vi.fn((url: string, init?: RequestInit) => {
      const method = init?.method ?? "GET";
      if (url.endsWith("/api/skills")) {
        return Promise.resolve(
          makeResponse(200, [{ name: "greet", source: "global" }]),
        );
      }
      if (url.includes("/skills") && method === "POST") {
        disk.add("greet");
        return Promise.resolve(makeResponse(200, { assigned: "greet" }));
      }
      if (url.includes("/skills")) {
        return Promise.resolve(
          makeResponse(200, [...disk].map((name) => ({ name, source: "global" }))),
        );
      }
      return Promise.resolve(makeResponse(200, []));
    });
    vi.stubGlobal("fetch", fetchMock);
    await renderSelector();
    fireEvent.click(screen.getByText(/Skills/));
    await waitFor(() =>
      expect((screen.getByLabelText("greet") as HTMLInputElement).checked).toBe(false),
    );
    fireEvent.click(screen.getByLabelText("greet"));
    await waitFor(() =>
      expect((screen.getByLabelText("greet") as HTMLInputElement).checked).toBe(true),
    );
  });

  it("shows the 'Skill changes apply immediately.' note", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.resolve(makeResponse(200, []))),
    );
    await renderSelector();
    fireEvent.click(screen.getByText(/Skills/));
    await waitFor(() =>
      expect(screen.getByText("Skill changes apply immediately.")).toBeTruthy(),
    );
  });
});
