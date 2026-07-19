import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { ViewCrossfade } from "./ViewCrossfade";

describe("ViewCrossfade", () => {
  it("applies the crossfade enter class", () => {
    const { container } = render(
      <ViewCrossfade viewKey="chat">
        <p>chat</p>
      </ViewCrossfade>,
    );
    const wrapper = container.firstElementChild as HTMLElement;
    expect(wrapper.className).toContain("view-crossfade-enter");
    expect(screen.getByText("chat")).toBeTruthy();
  });

  it("fills the parent (flex-1 + min-h-0) so the view owns the area", () => {
    const { container } = render(
      <ViewCrossfade viewKey="settings">
        <p>settings</p>
      </ViewCrossfade>,
    );
    const wrapper = container.firstElementChild as HTMLElement;
    expect(wrapper.className).toContain("flex-1");
    expect(wrapper.className).toContain("min-h-0");
  });

  it("remounts when viewKey changes (key-driven animation replay)", () => {
    const { container, rerender } = render(
      <ViewCrossfade viewKey="chat">
        <p>chat</p>
      </ViewCrossfade>,
    );
    const first = container.firstElementChild as HTMLElement;
    rerender(
      <ViewCrossfade viewKey="settings">
        <p>settings</p>
      </ViewCrossfade>,
    );
    const second = container.firstElementChild as HTMLElement;
    // React's key prop remounts the node on viewKey change — a fresh DOM node
    // is created, which is what re-triggers the CSS enter animation.
    expect(second).not.toBe(first);
    expect(screen.getByText("settings")).toBeTruthy();
  });
});
