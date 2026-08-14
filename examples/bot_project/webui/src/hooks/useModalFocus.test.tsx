// useModalFocus.test.tsx — contract tests for the modal focus trap shared by
// RunGraphModal and NewInstanceModal: initial focus target, Esc on the bubble phase
// (an Esc handled deeper must NOT close the modal), Tab wrap in both
// directions, and focus restore on unmount.

import { describe, it, expect, vi } from "vitest";
import { render, fireEvent, screen } from "@testing-library/react";
import { useRef } from "react";
import { useModalFocus } from "./useModalFocus";

function Harness({
  onClose,
  withInitial = false,
}: {
  onClose: () => void;
  withInitial?: boolean;
}) {
  const dialogRef = useRef<HTMLDivElement | null>(null);
  const initialRef = useRef<HTMLButtonElement | null>(null);
  useModalFocus({
    dialogRef,
    onClose,
    initialFocusRef: withInitial ? initialRef : undefined,
  });
  return (
    <div ref={dialogRef} tabIndex={-1} data-testid="dialog">
      {withInitial ? (
        <button type="button" ref={initialRef} data-testid="initial">
          initial
        </button>
      ) : null}
      <button type="button" data-testid="first">
        first
      </button>
      <button type="button" data-testid="last">
        last
      </button>
    </div>
  );
}

function EmptyHarness({ onClose }: { onClose: () => void }) {
  const dialogRef = useRef<HTMLDivElement | null>(null);
  useModalFocus({ dialogRef, onClose });
  return <div ref={dialogRef} tabIndex={-1} data-testid="dialog" />;
}

describe("useModalFocus", () => {
  it("focuses the dialog itself when no initialFocusRef is provided", () => {
    render(<Harness onClose={vi.fn()} />);
    expect(document.activeElement).toBe(screen.getByTestId("dialog"));
  });

  it("focuses initialFocusRef when provided", () => {
    render(<Harness onClose={vi.fn()} withInitial />);
    expect(document.activeElement).toBe(screen.getByTestId("initial"));
  });

  it("calls onClose on Escape", () => {
    const onClose = vi.fn();
    render(<Harness onClose={onClose} />);
    fireEvent.keyDown(window, { key: "Escape" });
    expect(onClose).toHaveBeenCalledOnce();
  });

  it("does not close when a deeper handler already stopped propagation (nested listbox Esc)", () => {
    const onClose = vi.fn();
    render(<Harness onClose={onClose} />);
    const inner = screen.getByTestId("first");
    inner.addEventListener("keydown", (e) => e.stopPropagation());
    fireEvent.keyDown(inner, { key: "Escape" });
    expect(onClose).not.toHaveBeenCalled();
  });

  it("wraps Tab from the last focusable element to the first", () => {
    render(<Harness onClose={vi.fn()} />);
    screen.getByTestId("last").focus();
    fireEvent.keyDown(window, { key: "Tab" });
    expect(document.activeElement).toBe(screen.getByTestId("first"));
  });

  it("wraps Shift+Tab from the first focusable element to the last", () => {
    render(<Harness onClose={vi.fn()} />);
    screen.getByTestId("first").focus();
    fireEvent.keyDown(window, { key: "Tab", shiftKey: true });
    expect(document.activeElement).toBe(screen.getByTestId("last"));
  });

  it("preventDefault-locks Tab when the dialog has no focusable elements", () => {
    render(<EmptyHarness onClose={vi.fn()} />);
    const event = new KeyboardEvent("keydown", {
      key: "Tab",
      bubbles: true,
      cancelable: true,
    });
    window.dispatchEvent(event);
    expect(event.defaultPrevented).toBe(true);
  });

  it("restores focus to the previously focused element on unmount", () => {
    const opener = document.createElement("button");
    document.body.appendChild(opener);
    opener.focus();
    const { unmount } = render(<Harness onClose={vi.fn()} />);
    expect(document.activeElement).toBe(screen.getByTestId("dialog"));
    unmount();
    expect(document.activeElement).toBe(opener);
    opener.remove();
  });
});
