import { describe, it, expect, vi, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { GlobalSkillsView, buildUpload } from "./GlobalSkillsView";
import { ToastProvider } from "../ToastContext";

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
    expect(screen.getByText("global")).toBeTruthy();
    expect(screen.getByText("local")).toBeTruthy();
  });

  it("Delete calls deleteSkill and removes the row after confirm", async () => {
    const fetchMock = vi.fn((url: string, init?: RequestInit) => {
      const method = init?.method ?? "GET";
      if (url === "/api/skills" && method === "DELETE") {
        return Promise.resolve(makeResponse(200, { deleted: "fmt" }));
      }
      return Promise.resolve(
        makeResponse(200, [{ name: "fmt", source: "global" }]),
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
    fireEvent.click(screen.getByText("weather"));
    await waitFor(() => expect(screen.getByText("Get weather forecasts.")).toBeTruthy());
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
    const row = screen.getByText("weather", { selector: "span" });
    fireEvent.click(row);
    await waitFor(() => expect(screen.getByText("Get weather forecasts.")).toBeTruthy());
    fireEvent.click(row);
    await waitFor(() =>
      expect(screen.queryByText("Get weather forecasts.")).toBeNull(),
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
    fireEvent.click(screen.getByText("weather", { selector: "span" }));
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Close" })).toBeTruthy(),
    );
    fireEvent.click(screen.getByRole("button", { name: "Close" }));
    await waitFor(() =>
      expect(screen.queryByText("Get weather forecasts.")).toBeNull(),
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
    fireEvent.click(screen.getByText("alpha", { selector: "span" }));
    await waitFor(() => expect(screen.getByText("Alpha desc.")).toBeTruthy());
    const alpha = screen.getByText("alpha", { selector: "span" }).closest("li")!;
    const beta = screen.getByText("beta", { selector: "span" }).closest("li")!;
    expect(alpha.textContent).toContain("Alpha desc.");
    expect(beta.textContent).not.toContain("Alpha desc.");
  });
});