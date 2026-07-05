import { describe, it, expect, vi, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { PromptEditor } from "./PromptEditor";
import { ToastProvider } from "../ToastContext";

function makeResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

const baseContent = { name: "main", content: "You are a helpful agent." };

afterEach(() => vi.unstubAllGlobals());

function renderEditor(onClose = () => {}): void {
  render(
    <ToastProvider>
      <PromptEditor pool="default" agent="main" onClose={onClose} />
    </ToastProvider>,
  );
}

describe("PromptEditor", () => {
  it("loads content into the textarea", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.resolve(makeResponse(200, baseContent))),
    );
    renderEditor();
    await waitFor(() =>
      expect((screen.getByRole("textbox") as HTMLTextAreaElement).value).toBe(
        "You are a helpful agent.",
      ),
    );
  });

  it("Save disabled until dirty; editing enables it", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.resolve(makeResponse(200, baseContent))),
    );
    renderEditor();
    await waitFor(() => expect(screen.getByRole("textbox")).toBeTruthy());
    const saveBtn = screen.getByText("Save") as HTMLButtonElement;
    expect(saveBtn.disabled).toBe(true);
    fireEvent.change(screen.getByRole("textbox"), {
      target: { value: "You are great." },
    });
    expect(saveBtn.disabled).toBe(false);
  });

  it("Save calls savePrompt and surfaces restart toast", async () => {
    const fetchMock = vi.fn((url: string, init?: RequestInit) => {
      const method = init?.method ?? "GET";
      if (url.includes("/prompt") && method === "PUT") {
        return Promise.resolve(
          makeResponse(200, {
            name: "main",
            content: init ? String(init.body) : "",
          }),
        );
      }
      return Promise.resolve(makeResponse(200, baseContent));
    });
    vi.stubGlobal("fetch", fetchMock);
    renderEditor();
    await waitFor(() => expect(screen.getByRole("textbox")).toBeTruthy());
    fireEvent.change(screen.getByRole("textbox"), {
      target: { value: "New prompt body." },
    });
    fireEvent.click(screen.getByText("Save"));
    await waitFor(() =>
      expect(screen.getByText("Saved. Restart to apply.")).toBeTruthy(),
    );
    // PUT was issued with the new content as the body
    type Call = [unknown, RequestInit?];
    const calls = fetchMock.mock.calls as unknown as Call[];
    const puts = calls.filter((c) => c[1]?.method === "PUT");
    expect(puts.length).toBeGreaterThanOrEqual(1);
    const body = String(puts[0]![1]!.body);
    expect(body).toContain("New prompt body.");
  });

  it("Cancel with unsaved edits shows a discard confirm dialog (not window.confirm)", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.resolve(makeResponse(200, baseContent))),
    );
    const onClose = vi.fn();
    renderEditor(onClose);
    await waitFor(() => expect(screen.getByRole("textbox")).toBeTruthy());
    fireEvent.change(screen.getByRole("textbox"), {
      target: { value: "edited" },
    });
    fireEvent.click(screen.getByText("Cancel"));
    // confirm dialog appears
    expect(screen.getByRole("dialog")).toBeTruthy();
    expect(screen.getByText("Discard unsaved changes?")).toBeTruthy();
    // onClose not yet called
    expect(onClose).not.toHaveBeenCalled();
    // confirm discard → onClose fires
    fireEvent.click(screen.getByRole("button", { name: "Discard" }));
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("Cancel with no edits closes immediately", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.resolve(makeResponse(200, baseContent))),
    );
    const onClose = vi.fn();
    renderEditor(onClose);
    await waitFor(() => expect(screen.getByRole("textbox")).toBeTruthy());
    fireEvent.click(screen.getByText("Cancel"));
    expect(onClose).toHaveBeenCalledTimes(1);
  });
});
