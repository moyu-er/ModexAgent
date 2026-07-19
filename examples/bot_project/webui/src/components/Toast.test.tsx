import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { Toast } from "./Toast";

describe("Toast", () => {
  it("renders the message on a popover surface with aria-live=polite", () => {
    render(<Toast message="Saved" onDismiss={() => {}} />);
    const el = screen.getByRole("status");
    expect(el.getAttribute("aria-live")).toBe("polite");
    expect(el.className).toContain("bg-canvas-popover");
    expect(el.className).toContain("border-hairline");
    expect(el.className).toContain("shadow-popover");
    expect(el.className).toContain("rounded-md");
    expect(el.className).toContain("toast-enter");
    expect(screen.getByText("Saved")).toBeTruthy();
  });

  it("severity dot follows the tone (brand/success, ember/warning, danger/error)", () => {
    const { container, rerender } = render(<Toast message="m" tone="info" onDismiss={() => {}} />);
    expect(container.querySelector(".bg-brand")).toBeTruthy();
    rerender(<Toast message="m" tone="success" onDismiss={() => {}} />);
    expect(container.querySelector(".bg-success")).toBeTruthy();
    rerender(<Toast message="m" tone="warning" onDismiss={() => {}} />);
    expect(container.querySelector(".bg-warning")).toBeTruthy();
    rerender(<Toast message="m" tone="error" onDismiss={() => {}} />);
    expect(container.querySelector(".bg-danger")).toBeTruthy();
  });

  it("severity is carried by the dot, not a full-tone border", () => {
    render(<Toast message="m" tone="error" onDismiss={() => {}} />);
    const el = screen.getByRole("status");
    expect(el.className).toContain("border-hairline");
    expect(el.className).not.toContain("border-danger");
  });

  it("dismiss button fires onDismiss", () => {
    const onDismiss = vi.fn();
    render(<Toast message="Bye" onDismiss={onDismiss} />);
    fireEvent.click(screen.getByRole("button", { name: "Dismiss" }));
    expect(onDismiss).toHaveBeenCalledTimes(1);
  });

  it("action click fires the action and dismisses", () => {
    const onDismiss = vi.fn();
    const onClick = vi.fn();
    render(
      <Toast message="Saved" action={{ label: "Undo", onClick }} onDismiss={onDismiss} />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Undo" }));
    expect(onClick).toHaveBeenCalledTimes(1);
    expect(onDismiss).toHaveBeenCalledTimes(1);
  });
});
