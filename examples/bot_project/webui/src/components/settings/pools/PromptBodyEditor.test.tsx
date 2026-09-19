// PromptBodyEditor (PA-12): direct prompt-body editing through the
// PromptStore REST owner — loads the referenced prompt, saves immediately
// on Save (NOT part of the scope draft), shows the shared-reference
// notice, and keeps the edited text on failure.

import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { describe, it, expect, vi, afterEach } from "vitest";
import { PromptBodyEditor } from "./PromptBodyEditor";
import { ToastProvider } from "../../ToastContext";

function makeResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), { status });
}

afterEach(() => vi.unstubAllGlobals());

function renderEditor(identityPersisted = true) {
  return render(
    <ToastProvider>
      <PromptBodyEditor promptName="coder" identityPersisted={identityPersisted} />
    </ToastProvider>,
  );
}

describe("PromptBodyEditor", () => {
  it("loads the prompt body and shows the shared-impact notice", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() =>
        Promise.resolve(makeResponse(200, { name: "coder", content: "You are a coder." })),
      ),
    );
    renderEditor();
    const textarea = await waitFor(() =>
      screen.getByLabelText("Working instructions"),
    ) as HTMLTextAreaElement;
    expect(textarea.value).toBe("You are a coder.");
    expect(
      screen.getByText("This prompt is shared — every agent referencing it sees your edit."),
    ).toBeTruthy();
    // Save disabled while clean.
    expect((screen.getByText("Save") as HTMLButtonElement).disabled).toBe(true);
  });

  it("Save PUTs the body immediately (not part of the scope draft)", async () => {
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith("/api/prompts/coder") && init?.method === "PUT") {
        return Promise.resolve(
          makeResponse(200, { name: "coder", content: String(init.body ? JSON.parse(String(init.body)).content : "") }),
        );
      }
      return Promise.resolve(
        makeResponse(200, { name: "coder", content: "You are a coder." }),
      );
    });
    vi.stubGlobal("fetch", fetchMock);
    renderEditor();
    const textarea = (await waitFor(() =>
      screen.getByLabelText("Working instructions"),
    )) as HTMLTextAreaElement;
    fireEvent.change(textarea, { target: { value: "Updated instructions." } });
    fireEvent.click(screen.getByText("Save"));
    await waitFor(() => {
      const puts = fetchMock.mock.calls.filter(
        (c) => String(c[0]).endsWith("/api/prompts/coder") && c[1]?.method === "PUT",
      );
      expect(puts).toHaveLength(1);
      expect(JSON.parse(String(puts[0]![1]?.body))).toEqual({
        content: "Updated instructions.",
      });
    });
    // The restart toast fires (prompt writes are restart-effective).
    await waitFor(() => {
      expect(screen.getByText("Saved. Restart to apply.")).toBeTruthy();
    });
  });

  it("a failed save keeps the edited text and shows an error", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.endsWith("/api/prompts/coder") && init?.method === "PUT") {
          return Promise.resolve(makeResponse(500, { error: "disk full" }));
        }
        return Promise.resolve(
          makeResponse(200, { name: "coder", content: "Original." }),
        );
      }),
    );
    renderEditor();
    const textarea = (await waitFor(() =>
      screen.getByLabelText("Working instructions"),
    )) as HTMLTextAreaElement;
    fireEvent.change(textarea, { target: { value: "Kept on failure." } });
    fireEvent.click(screen.getByText("Save"));
    await waitFor(() => {
      expect(screen.getByText(/Could not save the prompt body/)).toBeTruthy();
    });
    expect(textarea.value).toBe("Kept on failure.");
  });

  it("hides editing until the agent identity is persisted", () => {
    vi.stubGlobal("fetch", vi.fn());
    renderEditor(false);
    expect(
      screen.getByText("Save the assistant first — this action needs a persisted identity."),
    ).toBeTruthy();
    expect(screen.queryByLabelText("Working instructions")).toBeNull();
  });
});
