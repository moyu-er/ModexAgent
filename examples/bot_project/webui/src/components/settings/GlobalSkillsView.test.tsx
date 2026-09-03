import { describe, it, expect, vi, afterEach } from "vitest";
import {
  render,
  screen,
  fireEvent,
  waitFor,
  within,
} from "@testing-library/react";
import { GlobalSkillsView, buildUpload } from "./GlobalSkillsView";
import { ToastProvider } from "../ToastContext";
import { WorkspaceTabBar } from "../WorkspaceTabBar";

function makeResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

afterEach(() => vi.unstubAllGlobals());

function renderView(): void {
  render(
    <ToastProvider>
      <GlobalSkillsView />
    </ToastProvider>,
  );
}

function renderViewWithRestartIndicator(): void {
  const noop = (): void => {};
  render(
    <ToastProvider>
      <GlobalSkillsView />
      <WorkspaceTabBar
        tabs={[{ id: "__home__", path: "/home" }]}
        activeId="__home__"
        statuses={{}}
        home="/home"
        recentWorkspaces={[]}
        onOpenWorkspace={noop}
        onOpenRecent={noop}
        onActivate={noop}
        onClose={noop}
        onReorder={noop}
        onOpenSettings={noop}
      />
    </ToastProvider>,
  );
}

function makeSkillFile(
  contents: string,
  name: string,
  webkitRelativePath: string,
): File {
  const f = new File([contents], name, { type: "text/plain" });
  Object.defineProperty(f, "webkitRelativePath", {
    value: webkitRelativePath,
    configurable: true,
  });
  return f;
}

describe("GlobalSkillsView", () => {
  const topology = {
    kind: "workspace",
    workspace: "bot",
    pools: [
      {
        name: "default",
        peers: [],
        agents: [
          { name: "main", parent: null, root: true },
          { name: "helper", parent: "main", root: false },
        ],
      },
      {
        name: "coder",
        peers: [],
        agents: [
          { name: "orchestrator", parent: null, root: true },
          { name: "explore", parent: "orchestrator", root: false },
        ],
      },
    ],
  };

  it("selects pool and agent context and distinguishes global from assigned skills", async () => {
    const fetchMock = vi.fn((url: string) => {
      if (url === "/api/skills") {
        return Promise.resolve(
          makeResponse(200, [
            { name: "fmt", source: "global" },
            { name: "lint", source: "global" },
          ]),
        );
      }
      if (url === "/api/scope/topology") {
        return Promise.resolve(makeResponse(200, topology));
      }
      if (url === "/api/pools/default/agents/main/skills") {
        return Promise.resolve(
          makeResponse(200, [
            { name: "fmt", source: "global" },
            { name: "scratchpad", source: "local" },
          ]),
        );
      }
      if (url === "/api/pools/coder/agents/orchestrator/skills") {
        return Promise.resolve(makeResponse(200, []));
      }
      if (url === "/api/pools/coder/agents/explore/skills") {
        return Promise.resolve(
          makeResponse(200, [{ name: "lint", source: "global" }]),
        );
      }
      return Promise.resolve(makeResponse(404, {}));
    });
    vi.stubGlobal("fetch", fetchMock);
    renderView();

    const assignments = await screen.findByRole("region", {
      name: "Agent assignments",
    });
    const library = screen.getByRole("region", { name: "Global library" });
    await waitFor(() =>
      expect(
        (within(assignments).getByLabelText("fmt") as HTMLInputElement).checked,
      ).toBe(true),
    );
    expect(
      (within(assignments).getByLabelText("lint") as HTMLInputElement).checked,
    ).toBe(false);
    expect(within(assignments).queryByLabelText("scratchpad")).toBeNull();
    expect(within(assignments).getByText("scratchpad")).toBeTruthy();
    expect(within(library).getByText("fmt")).toBeTruthy();
    expect(within(library).getByText("lint")).toBeTruthy();

    fireEvent.click(screen.getByLabelText("Pool"));
    fireEvent.click(screen.getByRole("option", { name: "coder" }));
    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        "/api/pools/coder/agents/orchestrator/skills",
      ),
    );

    fireEvent.click(screen.getByLabelText("Agent"));
    fireEvent.click(screen.getByRole("option", { name: "explore" }));
    await waitFor(() =>
      expect(
        (within(assignments).getByLabelText("lint") as HTMLInputElement).checked,
      ).toBe(true),
    );
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/pools/coder/agents/explore/skills",
    );
  });

  it("assigns and unassigns immediately, then refreshes state from the agent listing", async () => {
    const disk = new Set<string>(["fmt"]);
    const collectionUrl = "/api/pools/default/agents/main/skills";
    const skillUrl = `${collectionUrl}/fmt`;
    const fetchMock = vi.fn((url: string, init?: RequestInit) => {
      const method = init?.method ?? "GET";
      if (url === "/api/skills") {
        return Promise.resolve(
          makeResponse(200, [{ name: "fmt", source: "global" }]),
        );
      }
      if (url === "/api/scope/topology") {
        return Promise.resolve(makeResponse(200, topology));
      }
      if (url === skillUrl && method === "DELETE") {
        disk.delete("fmt");
        return Promise.resolve(makeResponse(200, { unassigned: "fmt" }));
      }
      if (url === skillUrl && method === "POST") {
        disk.add("fmt");
        return Promise.resolve(makeResponse(200, { assigned: "fmt" }));
      }
      if (url === collectionUrl) {
        return Promise.resolve(
          makeResponse(
            200,
            [...disk].map((name) => ({ name, source: "global" })),
          ),
        );
      }
      return Promise.resolve(makeResponse(404, {}));
    });
    vi.stubGlobal("fetch", fetchMock);
    renderViewWithRestartIndicator();

    const assignments = await screen.findByRole("region", {
      name: "Agent assignments",
    });
    const checkbox = (await within(assignments).findByLabelText(
      "fmt",
    )) as HTMLInputElement;
    await waitFor(() => expect(checkbox.checked).toBe(true));
    expect(screen.queryByLabelText("Restart required")).toBeNull();

    fireEvent.click(checkbox);
    await waitFor(() => expect(checkbox.checked).toBe(false));
    expect(await screen.findByText('Unassigned skill "fmt".')).toBeTruthy();
    expect(screen.queryByLabelText("Restart required")).toBeNull();
    expect(screen.queryByRole("button", { name: "Restart now" })).toBeNull();
    expect(fetchMock).toHaveBeenCalledWith(skillUrl, { method: "DELETE" });

    fireEvent.click(checkbox);
    await waitFor(() => expect(checkbox.checked).toBe(true));
    expect(await screen.findByText('Assigned skill "fmt".')).toBeTruthy();
    expect(screen.queryByLabelText("Restart required")).toBeNull();
    expect(screen.queryByRole("button", { name: "Restart now" })).toBeNull();
    expect(fetchMock).toHaveBeenCalledWith(skillUrl, { method: "POST" });
    expect(
      fetchMock.mock.calls.filter(
        ([url, init]) => url === collectionUrl && !init?.method,
      ),
    ).toHaveLength(3);
  });

  it("shows assignment loading and errors, then retries with Refresh", async () => {
    let resolveInitialAgentLoad!: (response: Response) => void;
    const initialAgentLoad = new Promise<Response>((resolve) => {
      resolveInitialAgentLoad = resolve;
    });
    let agentLoads = 0;
    const fetchMock = vi.fn((url: string) => {
      if (url === "/api/skills") {
        return Promise.resolve(
          makeResponse(200, [{ name: "fmt", source: "global" }]),
        );
      }
      if (url === "/api/scope/topology") {
        return Promise.resolve(makeResponse(200, topology));
      }
      if (url === "/api/pools/default/agents/main/skills") {
        agentLoads += 1;
        if (agentLoads === 1) return initialAgentLoad;
        return Promise.resolve(
          makeResponse(200, [{ name: "fmt", source: "global" }]),
        );
      }
      return Promise.resolve(makeResponse(404, {}));
    });
    vi.stubGlobal("fetch", fetchMock);
    renderView();

    const assignments = await screen.findByRole("region", {
      name: "Agent assignments",
    });
    expect(await within(assignments).findByText("Loading…")).toBeTruthy();

    resolveInitialAgentLoad(makeResponse(503, { detail: "offline" }));
    const alert = await within(assignments).findByRole("alert");
    expect(alert.textContent).toContain("Failed to load");

    fireEvent.click(
      within(assignments).getByRole("button", {
        name: "Refresh assignments",
      }),
    );
    await waitFor(() =>
      expect(
        (within(assignments).getByLabelText("fmt") as HTMLInputElement).checked,
      ).toBe(true),
    );
    expect(within(assignments).queryByRole("alert")).toBeNull();
    expect(agentLoads).toBe(2);
  });

  it("keeps assignment state unchanged and surfaces API errors", async () => {
    const collectionUrl = "/api/pools/default/agents/main/skills";
    const skillUrl = `${collectionUrl}/fmt`;
    const fetchMock = vi.fn((url: string, init?: RequestInit) => {
      if (url === "/api/skills") {
        return Promise.resolve(
          makeResponse(200, [{ name: "fmt", source: "global" }]),
        );
      }
      if (url === "/api/scope/topology") {
        return Promise.resolve(makeResponse(200, topology));
      }
      if (url === collectionUrl) {
        return Promise.resolve(makeResponse(200, []));
      }
      if (url === skillUrl && init?.method === "POST") {
        return Promise.resolve(makeResponse(500, { detail: "nope" }));
      }
      return Promise.resolve(makeResponse(404, {}));
    });
    vi.stubGlobal("fetch", fetchMock);
    renderView();

    const assignments = await screen.findByRole("region", {
      name: "Agent assignments",
    });
    const checkbox = (await within(assignments).findByLabelText(
      "fmt",
    )) as HTMLInputElement;
    await waitFor(() => expect(checkbox.checked).toBe(false));
    fireEvent.click(checkbox);

    expect(await screen.findByText(/Skill assign failed: 500/)).toBeTruthy();
    expect(checkbox.checked).toBe(false);
    expect(fetchMock).toHaveBeenCalledWith(skillUrl, { method: "POST" });
  });

  it("renders the skill list from listSkills", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() =>
        Promise.resolve(
          makeResponse(200, [
            { name: "fmt", source: "global" },
            { name: "lint", source: "local" },
          ]),
        ),
      ),
    );
    renderView();
    await waitFor(() => expect(screen.getByText("fmt")).toBeTruthy());
    expect(screen.getByText("lint")).toBeTruthy();
  });

  it("Delete calls deleteSkill and removes the row after confirm", async () => {
    const fetchMock = vi.fn((url: string, init?: RequestInit) => {
      const method = init?.method ?? "GET";
      if (url === "/api/skills" && method === "DELETE") {
        return Promise.resolve(makeResponse(200, { deleted: "fmt" }));
      }
      return Promise.resolve(
        makeResponse(200, [{ name: "fmt", source: "global", origin: "repo" }]),
      );
    });
    vi.stubGlobal("fetch", fetchMock);
    renderView();
    await waitFor(() => expect(screen.getByText("fmt")).toBeTruthy());
    fireEvent.click(screen.getByText("fmt"));
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "Delete skill fmt" }),
      ).toBeTruthy(),
    );
    // Click the trash button — opens the ConfirmDialog.
    fireEvent.click(screen.getByRole("button", { name: "Delete skill fmt" }));
    // Confirm the deletion.
    const deleteBtn = await screen.findByText("Delete");
    fireEvent.click(deleteBtn);
    await waitFor(() => expect(screen.queryByText("fmt")).toBeNull());
    const deletes = fetchMock.mock.calls.filter(
      (c) => c[1]?.method === "DELETE",
    );
    expect(deletes.length).toBeGreaterThanOrEqual(1);
  });

  it("buildUpload derives name from top dir + rebases relpaths + base64-encodes", async () => {
    const fileA = makeSkillFile("hello", "SKILL.md", "fmt/SKILL.md");
    const fileB = makeSkillFile("#!/bin/bash", "run.sh", "fmt/run.sh");
    const out = await buildUpload([fileA, fileB]);
    expect(out).not.toBeNull();
    expect(out!.name).toBe("fmt");
    const names = out!.files.map((f) => f.relpath).sort();
    expect(names).toEqual(["SKILL.md", "run.sh"]);
    // base64 of "hello"
    expect(out!.files.find((f) => f.relpath === "SKILL.md")!.content).toBe(
      btoa("hello"),
    );
  });

  it("clicking the drop zone opens the directory picker via the hidden input", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() =>
        Promise.resolve(
          makeResponse(200, [{ name: "fmt", source: "global" }]),
        ),
      ),
    );
    renderView();
    await waitFor(() => expect(screen.getByText("fmt")).toBeTruthy());

    // The drop zone label is associated with the hidden directory input.
    const input = screen.getByLabelText(
      "Drop a directory here or click to upload",
    ) as HTMLInputElement;
    expect(input).toBeTruthy();
    expect(input.type).toBe("file");
    expect(input.hasAttribute("webkitdirectory")).toBe(true);
    expect(input.multiple).toBe(true);
    expect(input.classList.contains("hidden")).toBe(true);
  });

  it("selecting files via the hidden input shows a preview block with Confirm and Cancel", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() =>
        Promise.resolve(
          makeResponse(200, [{ name: "fmt", source: "global" }]),
        ),
      ),
    );
    renderView();
    await waitFor(() => expect(screen.getByText("fmt")).toBeTruthy());

    const input = document.getElementById("skill-upload") as HTMLInputElement;
    const fileA = makeSkillFile("hello", "SKILL.md", "fmt/SKILL.md");
    const fileB = makeSkillFile("#!/bin/bash", "run.sh", "fmt/run.sh");
    // Use Object.defineProperty to attach files because FileList isn't
    // constructible directly in happy-dom.
    Object.defineProperty(input, "files", {
      value: [fileA, fileB],
      configurable: true,
    });
    fireEvent.change(input);

    // Preview shows the derived skill name, file count, and bytes.
    expect(await screen.findByText("fmt")).toBeTruthy();
    expect(screen.getByText(/2 files/)).toBeTruthy();
    // Confirm + Cancel are rendered next to the preview.
    const confirmBtn = screen.getByRole("button", { name: "Confirm upload" });
    expect(confirmBtn).toBeTruthy();
    expect(screen.getByRole("button", { name: "Cancel" })).toBeTruthy();
  });

  it("Confirm upload triggers POST and clears the preview on success", async () => {
    const fetchMock = vi.fn((url: string, init?: RequestInit) => {
      if (url === "/api/skills" && init?.method === "POST") {
        return Promise.resolve(
          makeResponse(200, { name: "fmt", source: "global" }),
        );
      }
      return Promise.resolve(
        makeResponse(200, [{ name: "fmt", source: "global" }]),
      );
    });
    vi.stubGlobal("fetch", fetchMock);
    renderView();
    await waitFor(() => expect(screen.getByText("fmt")).toBeTruthy());

    const input = document.getElementById("skill-upload") as HTMLInputElement;
    const fileA = makeSkillFile("hello", "SKILL.md", "fmt/SKILL.md");
    Object.defineProperty(input, "files", {
      value: [fileA],
      configurable: true,
    });
    fireEvent.change(input);

    const confirmBtn = await screen.findByRole("button", {
      name: "Confirm upload",
    });
    fireEvent.click(confirmBtn);

    await waitFor(() => {
      const posts = fetchMock.mock.calls.filter(
        (c) => c[1]?.method === "POST" && c[0] === "/api/skills",
      );
      expect(posts.length).toBeGreaterThanOrEqual(1);
    });
    // Preview disappears after upload completes.
    await waitFor(() =>
      expect(screen.queryByRole("button", { name: "Confirm upload" })).toBeNull(),
    );
  });

  it("Cancel clears the preview and resets the file input", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() =>
        Promise.resolve(
          makeResponse(200, [{ name: "fmt", source: "global" }]),
        ),
      ),
    );
    renderView();
    await waitFor(() => expect(screen.getByText("fmt")).toBeTruthy());

    const input = document.getElementById("skill-upload") as HTMLInputElement;
    const fileA = makeSkillFile("hello", "SKILL.md", "fmt/SKILL.md");
    Object.defineProperty(input, "files", {
      value: [fileA],
      configurable: true,
    });
    fireEvent.change(input);

    // Preview shows Confirm/Cancel.
    await screen.findByRole("button", { name: "Confirm upload" });

    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));

    await waitFor(() =>
      expect(screen.queryByRole("button", { name: "Confirm upload" })).toBeNull(),
    );
    // File input is reset so re-selecting the same dir re-fires onChange.
    expect(input.value).toBe("");
  });

  it("drag-and-drop on the drop zone shows a preview", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() =>
        Promise.resolve(
          makeResponse(200, [{ name: "fmt", source: "global" }]),
        ),
      ),
    );
    renderView();
    await waitFor(() => expect(screen.getByText("fmt")).toBeTruthy());

    const zone = screen.getByText(
      "Drop a directory here or click to upload",
    );
    const fileA = makeSkillFile("hello", "SKILL.md", "fmt/SKILL.md");
    fireEvent.dragOver(zone);
    fireEvent.drop(zone, {
      dataTransfer: { files: [fileA] },
    });

    expect(await screen.findByRole("button", { name: "Confirm upload" })).toBeTruthy();
  });

  it("selects a skill and shows its description", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() =>
        Promise.resolve(
          makeResponse(200, [
            { name: "weather", source: "global", description: "Get weather forecasts." },
          ]),
        ),
      ),
    );
    renderView();
    await waitFor(() => expect(screen.getByText("weather")).toBeTruthy());
    // Click the first element matching "weather" (the row name span;
    // the description span also contains "weather" as substring).
    fireEvent.click(screen.getAllByText("weather")[0]!);
    await waitFor(() => expect(screen.getAllByText("Get weather forecasts.").length).toBeGreaterThan(0));
  });

  it("selects a skill without a description and shows the fallback", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() =>
        Promise.resolve(
          makeResponse(200, [{ name: "bare", source: "global" }]),
        ),
      ),
    );
    renderView();
    await waitFor(() => expect(screen.getByText("bare")).toBeTruthy());
    fireEvent.click(screen.getByText("bare"));
    await waitFor(() => expect(screen.getByText("No description.")).toBeTruthy());
  });

  it("clicking a selected skill again collapses the detail pane", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() =>
        Promise.resolve(
          makeResponse(200, [
            { name: "weather", source: "global", description: "Get weather forecasts." },
          ]),
        ),
      ),
    );
    renderView();
    await waitFor(() => expect(screen.getByText("weather")).toBeTruthy());
    const row = screen.getAllByText("weather")[0]!;
    fireEvent.click(row);
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Close" })).toBeTruthy(),
    );
    fireEvent.click(row);
    await waitFor(() =>
      expect(screen.queryByRole("button", { name: "Close" })).toBeNull(),
    );
  });

  it("Close button collapses the detail pane", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() =>
        Promise.resolve(
          makeResponse(200, [
            { name: "weather", source: "global", description: "Get weather forecasts." },
          ]),
        ),
      ),
    );
    renderView();
    await waitFor(() => expect(screen.getByText("weather")).toBeTruthy());
    fireEvent.click(screen.getAllByText("weather")[0]!);
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Close" })).toBeTruthy(),
    );
    fireEvent.click(screen.getByRole("button", { name: "Close" }));
    await waitFor(() =>
      expect(screen.queryByRole("button", { name: "Close" })).toBeNull(),
    );
    expect(
      screen.queryByRole("button", { name: "Delete skill weather" }),
    ).toBeNull();
  });

  it("detail pane renders inline within the selected skill's <li>", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() =>
        Promise.resolve(
          makeResponse(200, [
            { name: "alpha", source: "global", description: "Alpha desc." },
            { name: "beta", source: "global", description: "Beta desc." },
          ]),
        ),
      ),
    );
    renderView();
    await waitFor(() => expect(screen.getByText("alpha")).toBeTruthy());
    fireEvent.click(screen.getAllByText("alpha")[0]!);
    await waitFor(() => expect(screen.getAllByText("Alpha desc.").length).toBeGreaterThan(0));
    const alphaTexts = screen.getAllByText("alpha");
    const alpha = alphaTexts[0]!.closest("div")!.parentElement!;
    const beta = screen.getAllByText("beta")[0]!.closest("div")!.parentElement!;
    expect(alpha.textContent).toContain("Alpha desc.");
    expect(beta.textContent).not.toContain("Alpha desc.");
  });

  it("sorts skills alphabetically by name", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() =>
        Promise.resolve(
          makeResponse(200, [
            { name: "zebra", source: "global", origin: "repo" },
            { name: "apple", source: "global", origin: "user" },
            { name: "mango", source: "global", origin: "repo" },
          ]),
        ),
      ),
    );
    renderView();
    await waitFor(() => expect(screen.getByText("apple")).toBeTruthy());
    const buttons = screen.getAllByRole("button");
    const skillNames = buttons
      .map((b) => b.textContent ?? "")
      .map((t) => ["apple", "mango", "zebra"].find((n) => t.includes(n)))
      .filter((n): n is string => n !== undefined);
    expect(skillNames).toEqual(["apple", "mango", "zebra"]);
  });

  it("shows local badge for repo skills and global badge for user skills", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() =>
        Promise.resolve(
          makeResponse(200, [
            { name: "repo-skill", source: "global", origin: "repo" },
            { name: "user-skill", source: "global", origin: "user" },
          ]),
        ),
      ),
    );
    renderView();
    await waitFor(() => expect(screen.getByText("repo-skill")).toBeTruthy());
    expect(screen.getByText("local")).toBeTruthy();
    expect(screen.getByText("global")).toBeTruthy();
  });

  it("shows delete button only for repo skills when expanded", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() =>
        Promise.resolve(
          makeResponse(200, [
            { name: "repo-skill", source: "global", origin: "repo" },
            { name: "user-skill", source: "global", origin: "user" },
          ]),
        ),
      ),
    );
    renderView();
    await waitFor(() => expect(screen.getByText("repo-skill")).toBeTruthy());

    fireEvent.click(screen.getByText("repo-skill"));
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "Delete skill repo-skill" }),
      ).toBeTruthy(),
    );

    fireEvent.click(screen.getByText("user-skill"));
    await waitFor(() =>
      expect(
        screen.queryByRole("button", { name: "Delete skill user-skill" }),
      ).toBeNull(),
    );
  });
});
