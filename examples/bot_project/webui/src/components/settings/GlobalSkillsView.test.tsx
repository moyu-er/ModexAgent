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

  it("Delete calls deleteSkill and removes the row", async () => {
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
    fireEvent.click(screen.getByRole("button", { name: "Delete skill fmt" }));
    await waitFor(() => expect(screen.queryByText("fmt")).toBeNull());
    const deletes = fetchMock.mock.calls.filter(
      (c) => c[1]?.method === "DELETE",
    );
    expect(deletes.length).toBeGreaterThanOrEqual(1);
  });

  it("buildUpload derives name from top dir + rebases relpaths + base64-encodes", async () => {
    // Synthetic File list mimicking the directory picker output. webkitRelativePath
    // is populated by the picker; happy-dom does not simulate it, so we attach it.
    const fileA = new File(["hello"], "SKILL.md", { type: "text/plain" });
    Object.defineProperty(fileA, "webkitRelativePath", {
      value: "fmt/SKILL.md",
      configurable: true,
    });
    const fileB = new File(["#!/bin/bash"], "run.sh", { type: "text/x-sh" });
    Object.defineProperty(fileB, "webkitRelativePath", {
      value: "fmt/run.sh",
      configurable: true,
    });
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
});
