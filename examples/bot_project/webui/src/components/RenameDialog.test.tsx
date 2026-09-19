// RenameDialog — the one rename interaction shared by the sidebar and chat
// header entries (PA-02 DESIGN §2.6).
//
// Contract:
//  - Prefills the existing title; untitled sessions get an EMPTY input with
//    the full sessionId as placeholder.
//  - Save/Cancel buttons; Enter submits, Esc cancels.
//  - IME composition guard: Enter during composition (isComposing or
//    keyCode 229) never submits.
//  - While submitting, buttons disable and the input keeps its text.
//  - Failure (rejected save) preserves the input, closes nothing, and shows
//    an error message; blank input disables Save.
//  - Background refreshes never rewrite the field: the value is captured
//  - once on open (title prop changes do not clobber editing state).

import { describe, it, expect, vi, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { RenameDialog } from "./RenameDialog";

const baseProps = {
  sessionId: "abc123.main",
  title: undefined as string | undefined,
  onSave: vi.fn<(title: string) => Promise<void>>(),
  onCancel: vi.fn(),
};

afterEach(() => {
  vi.clearAllMocks();
});

describe("RenameDialog", () => {
  it("prefills the existing title", () => {
    render(<RenameDialog {...baseProps} title="Trip plan" />);
    const input = screen.getByRole("textbox") as HTMLInputElement;
    expect(input.value).toBe("Trip plan");
  });

  it("shows an empty input with the full sessionId placeholder when untitled", () => {
    render(<RenameDialog {...baseProps} />);
    const input = screen.getByRole("textbox") as HTMLInputElement;
    expect(input.value).toBe("");
    expect(input.placeholder).toBe("abc123.main");
  });

  it("Save button is disabled for blank/whitespace-only input", () => {
    render(<RenameDialog {...baseProps} title="Old" />);
    const input = screen.getByRole("textbox");
    const save = screen.getByRole("button", { name: "Save" });
    expect(save.hasAttribute("disabled")).toBe(false);

    fireEvent.change(input, { target: { value: "   " } });
    expect(save.hasAttribute("disabled")).toBe(true);

    fireEvent.change(input, { target: { value: "" } });
    expect(save.hasAttribute("disabled")).toBe(true);
  });

  it("Enter submits the trimmed title; Esc cancels", async () => {
    const onSave = vi.fn().mockResolvedValue(undefined);
    render(<RenameDialog {...baseProps} title="Old" onSave={onSave} />);
    const input = screen.getByRole("textbox");

    fireEvent.change(input, { target: { value: "  New name  " } });
    fireEvent.keyDown(input, { key: "Enter" });
    await waitFor(() => expect(onSave).toHaveBeenCalledWith("New name"));

    fireEvent.keyDown(input, { key: "Escape" });
    expect(baseProps.onCancel).toHaveBeenCalled();
  });

  it("Enter during IME composition does NOT submit", () => {
    const onSave = vi.fn().mockResolvedValue(undefined);
    render(<RenameDialog {...baseProps} title="Old" onSave={onSave} />);
    const input = screen.getByRole("textbox");

    fireEvent.change(input, { target: { value: "杭州" } });
    fireEvent.keyDown(input, { key: "Enter", isComposing: true });
    expect(onSave).not.toHaveBeenCalled();

    // Legacy Safari/Edge composition keyCode.
    fireEvent.keyDown(input, { key: "Enter", keyCode: 229 });
    expect(onSave).not.toHaveBeenCalled();

    // A real Enter after composition ends submits.
    fireEvent.keyDown(input, { key: "Enter", isComposing: false });
    expect(onSave).toHaveBeenCalledWith("杭州");
  });

  it("shows submitting state (disabled controls) while saving", async () => {
    let release!: () => void;
    const onSave = vi.fn(
      () =>
        new Promise<void>((resolve) => {
          release = resolve;
        }),
    );
    render(<RenameDialog {...baseProps} title="Old" onSave={onSave} />);
    const input = screen.getByRole("textbox");
    const save = screen.getByRole("button", { name: "Save" });

    fireEvent.change(input, { target: { value: "New" } });
    fireEvent.click(save);

    expect(save.hasAttribute("disabled")).toBe(true);
    expect(screen.getByRole("button", { name: "Cancel" }).hasAttribute("disabled")).toBe(true);

    release();
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Save" }).hasAttribute("disabled")).toBe(false),
    );
  });

  it("failure preserves the input text and surfaces an error; no cancel", async () => {
    const onSave = vi.fn().mockRejectedValue(new Error("API 400"));
    render(<RenameDialog {...baseProps} title="Old" onSave={onSave} />);
    const input = screen.getByRole("textbox");
    const save = screen.getByRole("button", { name: "Save" });

    fireEvent.change(input, { target: { value: "Keep me" } });
    fireEvent.click(save);

    await waitFor(() => expect(screen.getByText("Could not save the title.")).toBeTruthy());
    expect((screen.getByRole("textbox") as HTMLInputElement).value).toBe("Keep me");
    expect(baseProps.onCancel).not.toHaveBeenCalled();
    // Retry is possible with the same text.
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(onSave).toHaveBeenCalledTimes(2));
  });

  it("background title prop changes never rewrite the field mid-edit", () => {
    const { rerender } = render(<RenameDialog {...baseProps} title="Old" />);
    const input = screen.getByRole("textbox");
    fireEvent.change(input, { target: { value: "My edit" } });

    // A sessions_changed refresh lands with a new title prop — the dialog
    // is already open and editing; the user's text wins.
    rerender(<RenameDialog {...baseProps} title="Refreshed elsewhere" />);
    expect((screen.getByRole("textbox") as HTMLInputElement).value).toBe("My edit");
  });

  it("re-seeds the input when reopened for another session", () => {
    const { rerender } = render(
      <RenameDialog {...baseProps} sessionId="aaa.main" title="A" />,
    );
    rerender(
      <RenameDialog {...baseProps} sessionId="bbb.main" title="B" />,
    );
    expect((screen.getByRole("textbox") as HTMLInputElement).value).toBe("B");
  });
});
