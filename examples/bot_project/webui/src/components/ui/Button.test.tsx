import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { Button } from "./Button";

describe("Button", () => {
  it("renders a button with default type=button and spreads props", () => {
    render(
      <Button data-testid="b" aria-label="go" onClick={() => {}}>
        Save
      </Button>,
    );
    const el = screen.getByTestId("b");
    expect(el.tagName).toBe("BUTTON");
    expect(el.getAttribute("type")).toBe("button");
    expect(el.getAttribute("aria-label")).toBe("go");
  });

  it("primary variant uses the single .btn-primary gradient+glow implementation", () => {
    render(<Button variant="primary">Save</Button>);
    const el = screen.getByRole("button");
    expect(el.className).toContain("btn-primary");
    expect(el.className).toContain("rounded-md");
    expect(el.className).toContain("focus-visible:ring-brand");
    // Gradient/glow must NOT be re-implemented as utility classes.
    expect(el.className).not.toContain("bg-link");
    expect(el.className).not.toContain("bg-gradient");
  });

  it("secondary variant is the bordered neutral button with teal-shimmer hover border", () => {
    render(<Button variant="secondary">Save</Button>);
    const el = screen.getByRole("button");
    expect(el.className).toContain("bg-canvas-elevated");
    expect(el.className).toContain("border-hairline");
    expect(el.className).toContain("hover:border-border-strong");
  });

  it("ghost variant is borderless with a hover tint", () => {
    render(<Button variant="ghost">Save</Button>);
    const el = screen.getByRole("button");
    expect(el.className).toContain("bg-transparent");
    expect(el.className).toContain("hover:bg-hairline-soft");
  });

  it("danger variant is semantic danger, never a gradient", () => {
    render(<Button variant="danger">Delete</Button>);
    const el = screen.getByRole("button");
    expect(el.className).toContain("text-danger");
    expect(el.className).toContain("border-danger");
    expect(el.className).toContain("hover:bg-hairline-soft");
    expect(el.className).not.toContain("btn-primary");
  });

  it("non-link variants get hover lift and press scale", () => {
    render(<Button variant="secondary">Save</Button>);
    const el = screen.getByRole("button");
    expect(el.className).toContain("enabled:hover:-translate-y-px");
    expect(el.className).toContain("enabled:active:scale-[0.98]");
  });

  it("shape system uses md radius by default and pill when requested", () => {
    const { rerender } = render(<Button>Save</Button>);
    expect(screen.getByRole("button").className).toContain("rounded-md");
    rerender(<Button shape="pill">Save</Button>);
    expect(screen.getByRole("button").className).toContain("rounded-pill");
  });

  it("small buttons use the sm radius (radius scale)", () => {
    render(<Button size="sm">Save</Button>);
    expect(screen.getByRole("button").className).toContain("rounded-sm");
  });

  it("link variant uses the brand token, transparent bg, underline hover", () => {
    render(<Button variant="link">Help</Button>);
    const el = screen.getByRole("button");
    expect(el.className).toContain("text-brand");
    expect(el.className).toContain("hover:underline");
    expect(el.className).not.toContain("enabled:hover:-translate-y-px");
  });

  it("size mapping changes height; md is the 36px minimum", () => {
    const { rerender } = render(<Button size="sm">a</Button>);
    expect(screen.getByRole("button").className).toContain("h-7");
    rerender(<Button size="md">a</Button>);
    expect(screen.getByRole("button").className).toContain("h-9");
    rerender(<Button size="lg">a</Button>);
    expect(screen.getByRole("button").className).toContain("h-10");
  });

  it("disabled state applies 45% opacity and blocks click", () => {
    const onClick = vi.fn();
    render(
      <Button disabled onClick={onClick}>
        Save
      </Button>,
    );
    const el = screen.getByRole("button");
    expect(el.className).toContain("disabled:opacity-45");
    fireEvent.click(el);
    expect(onClick).not.toHaveBeenCalled();
  });

  it("loading state shows spinner, sets aria-busy, blocks click", () => {
    const onClick = vi.fn();
    render(
      <Button loading onClick={onClick}>
        Save
      </Button>,
    );
    const el = screen.getByRole("button");
    expect(el.getAttribute("aria-busy")).toBe("true");
    expect(el.querySelector("svg")).toBeTruthy();
    fireEvent.click(el);
    expect(onClick).not.toHaveBeenCalled();
  });

  it("fires onClick when interactive", () => {
    const onClick = vi.fn();
    render(<Button onClick={onClick}>Go</Button>);
    fireEvent.click(screen.getByRole("button"));
    expect(onClick).toHaveBeenCalledTimes(1);
  });
});
