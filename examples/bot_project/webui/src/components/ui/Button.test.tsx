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

  it("primary variant uses the Notion blue-on-surface tokens", () => {
    render(<Button variant="primary">Save</Button>);
    const el = screen.getByRole("button");
    expect(el.className).toContain("bg-link");
    expect(el.className).toContain("text-canvas-elevated");
    expect(el.className).toContain("rounded-sm");
    expect(el.className).toContain("focus-visible:ring-link/30");
  });

  it("shape system uses square radius by default and pill when requested", () => {
    const { rerender } = render(<Button>Save</Button>);
    expect(screen.getByRole("button").className).toContain("rounded-sm");
    rerender(<Button shape="pill">Save</Button>);
    expect(screen.getByRole("button").className).toContain("rounded-pill");
  });

  it("link variant uses Geist link token, transparent bg, underline hover", () => {
    render(<Button variant="link">Help</Button>);
    const el = screen.getByRole("button");
    expect(el.className).toContain("text-link");
    expect(el.className).toContain("hover:underline");
  });

  it("size mapping changes height", () => {
    const { rerender } = render(<Button size="sm">a</Button>);
    expect(screen.getByRole("button").className).toContain("h-7");
    rerender(<Button size="lg">a</Button>);
    expect(screen.getByRole("button").className).toContain("h-10");
  });

  it("disabled state applies reduced opacity and blocks click", () => {
    const onClick = vi.fn();
    render(
      <Button disabled onClick={onClick}>
        Save
      </Button>,
    );
    const el = screen.getByRole("button");
    expect(el.className).toContain("disabled:opacity-60");
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