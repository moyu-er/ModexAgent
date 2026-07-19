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

function renderEditor(
  onClose = () => {},
  onSave?: (content: string) => Promise<void>,
): void {
  render(
    <ToastProvider>
      <PromptEditor promptName="main" onClose={onClose} onSave={onSave} />
    </ToastProvider>,
  );
}

describe("PromptEditor", () => {
  it("loads content into the textarea via GET /api/prompts/{name}", async () => {
    const fetchMock = vi.fn((url: string) => {
      // The new global prompts endpoint, not the legacy pool-scoped one.
      expect(url).toBe("/api/prompts/main");
      return Promise.resolve(makeResponse(200, baseContent));
    });
    vi.stubGlobal("fetch", fetchMock);
    renderEditor(undefined, vi.fn().mockResolvedValue(undefined));
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
    renderEditor(undefined, vi.fn().mockResolvedValue(undefined));
    await waitFor(() => expect(screen.getByRole("textbox")).toBeTruthy());
    const saveBtn = screen.getByText("Save") as HTMLButtonElement;
    expect(saveBtn.disabled).toBe(true);
    fireEvent.change(screen.getByRole("textbox"), {
      target: { value: "You are great." },
    });
    expect(saveBtn.disabled).toBe(false);
  });

  it("Save calls onSave and surfaces restart toast", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.resolve(makeResponse(200, baseContent))),
    );
    const onSave = vi.fn().mockResolvedValue(undefined);
    renderEditor(undefined, onSave);
    await waitFor(() => expect(screen.getByRole("textbox")).toBeTruthy());
    fireEvent.change(screen.getByRole("textbox"), {
      target: { value: "New prompt body." },
    });
    fireEvent.click(screen.getByText("Save"));
    await waitFor(() =>
      expect(screen.getByText("Saved. Restart to apply.")).toBeTruthy(),
    );
    expect(onSave).toHaveBeenCalledWith("New prompt body.");
  });

  it("Cancel with unsaved edits shows a discard confirm dialog (not window.confirm)", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.resolve(makeResponse(200, baseContent))),
    );
    const onClose = vi.fn();
    renderEditor(onClose, vi.fn().mockResolvedValue(undefined));
    await waitFor(() => expect(screen.getByRole("textbox")).toBeTruthy());
    fireEvent.change(screen.getByRole("textbox"), {
      target: { value: "edited" },
    });
    fireEvent.click(screen.getByText("Cancel"));
    expect(screen.getByRole("dialog")).toBeTruthy();
    expect(screen.getByText("Discard unsaved changes?")).toBeTruthy();
    expect(onClose).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Discard" }));
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("Cancel with no edits closes immediately", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.resolve(makeResponse(200, baseContent))),
    );
    const onClose = vi.fn();
    renderEditor(onClose, vi.fn().mockResolvedValue(undefined));
    await waitFor(() => expect(screen.getByRole("textbox")).toBeTruthy());
    fireEvent.click(screen.getByText("Cancel"));
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("renders the slide-over header when one is provided", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.resolve(makeResponse(200, baseContent))),
    );
    render(
      <ToastProvider>
        <PromptEditor
          promptName="main"
          onClose={() => {}}
          onSave={vi.fn().mockResolvedValue(undefined)}
          slideOverHeader={<div data-testid="slide-over-header">Close me</div>}
        />
      </ToastProvider>,
    );
    await waitFor(() => expect(screen.getByTestId("slide-over-header")).toBeTruthy());
    expect(screen.getByRole("textbox")).toBeTruthy();
  });

  it("read-only mode (no onSave): textarea disabled, no Save button", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.resolve(makeResponse(200, baseContent))),
    );
    render(
      <ToastProvider>
        <PromptEditor promptName="main" />
      </ToastProvider>,
    );
    await waitFor(() => expect(screen.getByRole("textbox")).toBeTruthy());
    const textarea = screen.getByRole("textbox") as HTMLTextAreaElement;
    expect(textarea.disabled).toBe(true);
    expect(screen.queryByText("Save")).toBeNull();
    expect(screen.queryByText("Cancel")).toBeNull();
  });
});
