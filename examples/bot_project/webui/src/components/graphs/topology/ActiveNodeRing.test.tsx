import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { ActiveNodeRing } from "./ActiveNodeRing";
import { ringSlotGeometry, RING_SLOT_OUTSET, GRAPH_NODE_RADIUS } from "./GraphNode";

function renderRing(
  props: Partial<Parameters<typeof ActiveNodeRing>[0]> = {},
) {
  const defaults = { width: 140, height: 44, cx: 100, cy: 200 };
  return render(
    <svg>
      <ActiveNodeRing {...defaults} {...props} />
    </svg>,
  );
}

describe("ActiveNodeRing", () => {
  it("renders a rect with the correct ring-slot geometry (§4.4 外扩 4px)", () => {
    renderRing();
    const rect = screen.getByTestId("active-node-ring");
    expect(rect).toBeTruthy();
    expect(rect.tagName).toBe("rect");

    const geo = ringSlotGeometry(140, 44);
    expect(rect.getAttribute("x")).toBe(String(geo.x));
    expect(rect.getAttribute("y")).toBe(String(geo.y));
    expect(rect.getAttribute("width")).toBe(String(geo.width));
    expect(rect.getAttribute("height")).toBe(String(geo.height));
    expect(rect.getAttribute("rx")).toBe(String(geo.rx));
  });

  it("uses the active-ring stroke token and stroke-width 2, fill none", () => {
    const { container } = renderRing();
    const rect = container.querySelector("rect")!;
    expect(rect.getAttribute("stroke")).toBe("var(--color-graph-active-ring)");
    expect(rect.getAttribute("stroke-width")).toBe("2");
    expect(rect.getAttribute("fill")).toBe("none");
  });

  it("applies the graph-ring-pulse CSS class for the animation", () => {
    const { container } = renderRing();
    const rect = container.querySelector("rect")!;
    expect(rect.getAttribute("class")).toContain("graph-ring-pulse");
  });

  it("is positioned at the node center via translate transform", () => {
    const { container } = renderRing({ cx: 250, cy: 300 });
    const rect = container.querySelector("rect")!;
    expect(rect.getAttribute("transform")).toBe("translate(250 300)");
  });

  it("sets pointerEvents=none so the ring does not intercept node clicks", () => {
    const { container } = renderRing();
    const rect = container.querySelector("rect")!;
    expect(rect.getAttribute("pointer-events")).toBe("none");
  });

  it("adapts geometry for different node sizes (start/end nodes)", () => {
    // START node: 20×20
    const { container: startContainer } = renderRing({
      width: 20,
      height: 20,
      cx: 0,
      cy: 0,
    });
    const startRect = startContainer.querySelector("rect")!;
    const startGeo = ringSlotGeometry(20, 20);
    expect(startRect.getAttribute("x")).toBe(String(startGeo.x));
    expect(startRect.getAttribute("width")).toBe(String(startGeo.width));

    // END node: 4×20
    const { container: endContainer } = renderRing({
      width: 4,
      height: 20,
      cx: 50,
      cy: 50,
    });
    const endRect = endContainer.querySelector("rect")!;
    const endGeo = ringSlotGeometry(4, 20);
    expect(endRect.getAttribute("x")).toBe(String(endGeo.x));
    expect(endRect.getAttribute("width")).toBe(String(endGeo.width));
  });

  it("geometry is outset by RING_SLOT_OUTSET (4px) on each side", () => {
    const w = 140;
    const h = 44;
    const { container } = renderRing({ width: w, height: h, cx: 0, cy: 0 });
    const rect = container.querySelector("rect")!;
    // width = w + 2*outset, height = h + 2*outset
    expect(Number(rect.getAttribute("width"))).toBe(w + RING_SLOT_OUTSET * 2);
    expect(Number(rect.getAttribute("height"))).toBe(h + RING_SLOT_OUTSET * 2);
    // rx = radius-md + outset = 12 + 4 = 16
    expect(Number(rect.getAttribute("rx"))).toBe(
      GRAPH_NODE_RADIUS + RING_SLOT_OUTSET,
    );
  });
});
