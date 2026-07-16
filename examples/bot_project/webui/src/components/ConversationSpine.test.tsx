import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { createRef } from "react";
import {
  ConversationSpine,
  computeActiveIndex,
  jumpTargetTop,
  type SpineAnchor,
} from "./ConversationSpine";

describe("computeActiveIndex (pure)", () => {
  it("returns -1 for empty ratios", () => {
    expect(computeActiveIndex([], 0.5)).toBe(-1);
  });

  it("skips non-finite ratios", () => {
    const ratios = [NaN, 0.4, 0.8];
    // viewport center at 0.4 → nearest finite is 0.4 (index 1)
    expect(computeActiveIndex(ratios, 0.4)).toBe(1);
  });

  it("picks the nearest ratio", () => {
    const ratios = [0.1, 0.5, 0.9];
    expect(computeActiveIndex(ratios, 0.0)).toBe(0);
    expect(computeActiveIndex(ratios, 0.45)).toBe(1);
    expect(computeActiveIndex(ratios, 0.95)).toBe(2);
  });

  it("picks the nearer of two anchors", () => {
    const ratios = [0.2, 0.6];
    expect(computeActiveIndex(ratios, 0.5)).toBe(1); // 0.6 nearer than 0.2
    expect(computeActiveIndex(ratios, 0.25)).toBe(0); // 0.2 nearer than 0.6
  });
});

describe("jumpTargetTop (pure)", () => {
  it("places the anchor center in the viewport center", () => {
    // anchor at 50% of a 1000px content, viewport 400px → center 500,
    // target = 500 - 200 = 300
    expect(jumpTargetTop(0.5, 1000, 400)).toBe(300);
  });

  it("clamps to 0 at the top", () => {
    expect(jumpTargetTop(0, 1000, 400)).toBe(0);
    expect(jumpTargetTop(0.1, 1000, 400)).toBe(0); // 100 - 200 = -100 → 0
  });

  it("clamps to maxScroll at the bottom", () => {
    // anchor at 100%, content 1000, viewport 400 → max = 600
    expect(jumpTargetTop(1, 1000, 400)).toBe(600);
    expect(jumpTargetTop(0.95, 1000, 400)).toBe(600); // 950 - 200 = 750 → 600
  });

  it("returns 0 for non-finite ratio or zero height", () => {
    expect(jumpTargetTop(NaN, 1000, 400)).toBe(0);
    expect(jumpTargetTop(0.5, 0, 400)).toBe(0);
  });
});

describe("ConversationSpine (render)", () => {
  const baseAnchor: SpineAnchor = { id: "u1", preview: "hello world" };

  it("renders nothing when there are no anchors", () => {
    const scrollRef = createRef<HTMLDivElement>();
    const contentRef = createRef<HTMLDivElement>();
    const { container } = render(
      <ConversationSpine scrollRef={scrollRef} contentRef={contentRef} anchors={[]} />,
    );
    expect(container.firstChild).toBeNull();
  });

  it("renders one button per anchor", () => {
    const scrollRef = createRef<HTMLDivElement>();
    const contentRef = createRef<HTMLDivElement>();
    const anchors: SpineAnchor[] = [
      { id: "u1", preview: "first question" },
      { id: "u2", preview: "second question" },
      { id: "u3", preview: "third question" },
    ];
    render(
      <ConversationSpine scrollRef={scrollRef} contentRef={contentRef} anchors={anchors} />,
    );
    // Dots are visibility:hidden when no real scroll DOM is attached (ratios
    // are NaN), so query the accessibility tree with hidden:true.
    const buttons = screen.getAllByRole("button", { hidden: true });
    expect(buttons.length).toBe(3);
    expect(buttons[0]!.getAttribute("aria-label")).toContain("first question");
    expect(buttons[2]!.getAttribute("aria-label")).toContain("third question");
  });

  it("does not crash when refs are null", () => {
    const scrollRef = createRef<HTMLDivElement>();
    const contentRef = createRef<HTMLDivElement>();
    render(
      <ConversationSpine
        scrollRef={scrollRef}
        contentRef={contentRef}
        anchors={[baseAnchor]}
      />,
    );
    expect(screen.getAllByRole("button", { hidden: true }).length).toBe(1);
  });
});
