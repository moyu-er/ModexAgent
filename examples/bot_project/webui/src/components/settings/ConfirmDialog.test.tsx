import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { ConfirmDialog } from "./ConfirmDialog";

describe("ConfirmDialog", () => {
  it("renders title + message and exposes role=dialog", () => {
    render(
      <ConfirmDialog
        title="Delete pool?"
        message="This cannot be undone."
        onConfirm={() => {}}
        onCancel={() => {}}
      />,
    );
    expect(screen.getByRole("dialog")).toBeTruthy();
    expect(screen.getByText("Delete pool?")).toBeTruthy();
    expect(screen.getByText("This cannot be undone.")).toBeTruthy();
  });

  it("Confirm fires onConfirm; Cancel fires onCancel", () => {
    const onConfirm = vi.fn();
    const onCancel = vi.fn();
    render(
      <ConfirmDialog
        title="Discard?"
        confirmLabel="Discard"
        onConfirm={onConfirm}
        onCancel={onCancel}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Discard" }));
    expect(onConfirm).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(onCancel).toHaveBeenCalledTimes(1);
  });

  it("danger tone renders the confirm button with danger styling", () => {
    render(
      <ConfirmDialog
        title="Delete"
        confirmLabel="Delete"
        tone="danger"
        onConfirm={() => {}}
        onCancel={() => {}}
      />,
    );
    const btn = screen.getByRole("button", { name: "Delete" });
    expect(btn.className).toContain("text-danger");
  });

  it("panel uses the modal spec: popover surface, lg radius, scale+fade entry", () => {
    render(<ConfirmDialog title="Discard?" onConfirm={() => {}} onCancel={() => {}} />);
    const dialog = screen.getByRole("dialog");
    expect(dialog.className).toContain("bg-overlay");
    expect(dialog.className).toContain("modal-scrim-enter");
    const panel = dialog.querySelector(".modal-panel-enter") as HTMLElement;
    expect(panel).toBeTruthy();
    expect(panel.className).toContain("rounded-lg");
    expect(panel.className).toContain("bg-canvas-popover");
    expect(panel.className).toContain("shadow-popover");
  });

  it("Escape key fires onCancel", () => {
    const onCancel = vi.fn();
    render(
      <ConfirmDialog
        title="Discard?"
        confirmLabel="Discard"
        onConfirm={() => {}}
        onCancel={onCancel}
      />,
    );
    fireEvent.keyDown(window, { key: "Escape" });
    expect(onCancel).toHaveBeenCalledTimes(1);
  });
});