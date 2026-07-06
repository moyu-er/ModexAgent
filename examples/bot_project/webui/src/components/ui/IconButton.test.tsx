import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { IconButton } from "./IconButton";

const Icon = () => <svg data-testid="icon" />;

describe("IconButton", () => {
  it("uses label as aria-label and title", () => {
    render(<IconButton icon={<Icon />} label="Delete" />);
    const el = screen.getByRole("button", { name: "Delete" });
    expect(el.getAttribute("title")).toBe("Delete");
    expect(el.querySelector('[data-testid="icon"]')).toBeTruthy();
  });

  it("respects explicit aria-label override", () => {
    render(<IconButton icon={<Icon />} label="Delete" aria-label="Remove row" />);
    const el = screen.getByRole("button", { name: "Remove row" });
    expect(el.getAttribute("aria-label")).toBe("Remove row");
  });

  it("size mapping picks circular dimensions", () => {
    const { rerender } = render(<IconButton icon={<Icon />} label="x" size="sm" />);
    expect(screen.getByRole("button").className).toContain("h-7 w-7");
    rerender(<IconButton icon={<Icon />} label="x" size="md" />);
    expect(screen.getByRole("button").className).toContain("h-8 w-8");
  });

  it("is circular and uses the link focus ring", () => {
    render(<IconButton icon={<Icon />} label="x" />);
    const el = screen.getByRole("button");
    expect(el.className).toContain("rounded-full");
    expect(el.className).toContain("focus-visible:ring-link/30");
  });

  it("variant mapping applies Geist color classes", () => {
    const { rerender } = render(<IconButton icon={<Icon />} label="x" variant="primary" />);
    expect(screen.getByRole("button").className).toContain("bg-ink");
    rerender(<IconButton icon={<Icon />} label="x" variant="secondary" />);
    expect(screen.getByRole("button").className).toContain("border-hairline");
    rerender(<IconButton icon={<Icon />} label="x" variant="ghost" />);
    expect(screen.getByRole("button").className).toContain("text-body");
  });

  it("disabled blocks click", () => {
    const onClick = vi.fn();
    render(<IconButton icon={<Icon />} label="x" disabled onClick={onClick} />);
    fireEvent.click(screen.getByRole("button"));
    expect(onClick).not.toHaveBeenCalled();
  });

  it("forwards onClick when interactive", () => {
    const onClick = vi.fn();
    render(<IconButton icon={<Icon />} label="x" onClick={onClick} />);
    fireEvent.click(screen.getByRole("button"));
    expect(onClick).toHaveBeenCalledTimes(1);
  });
});
