import { describe, it, expect } from "vitest";
import { render } from "@testing-library/react";
import { GraphEdge, edgePathD } from "./GraphEdge";

const POINTS = [
  { x: 0, y: 0 },
  { x: 0, y: 30 },
  { x: 40, y: 60 },
];

function renderEdge(props: Partial<Parameters<typeof GraphEdge>[0]> = {}) {
  return render(
    <svg>
      <GraphEdge source="a" target="b" points={POINTS} {...props} />
    </svg>,
  );
}

describe("edgePathD", () => {
  it("builds a polyline path through the dagre points", () => {
    expect(edgePathD(POINTS)).toBe("M0 0 L0 30 L40 60");
  });
});

describe("GraphEdge", () => {
  it("renders a 1.5px border-strong stroke with a same-color arrow marker (§5.3)", () => {
    const { container } = renderEdge();
    const edge = container.querySelector('[data-testid="graph-edge-a-b"]')!;
    const path = edge.querySelector("path[fill='none']")!;
    expect(path.getAttribute("d")).toBe("M0 0 L0 30 L40 60");
    expect(path.getAttribute("stroke-width")).toBe("1.5");
    expect(path.getAttribute("class")).toContain("stroke-graph-edge");
    expect(path.getAttribute("marker-end")).toMatch(/^url\(#graph-arrow-/);
    const arrow = edge.querySelector("marker path")!;
    expect(arrow.getAttribute("class")).toContain("fill-graph-arrow");
    const marker = edge.querySelector("marker")!;
    expect(marker.getAttribute("markerWidth")).toBe("6");
    expect(marker.getAttribute("markerHeight")).toBe("6");
  });

  it("active state switches stroke and arrow to the active tokens", () => {
    const { container } = renderEdge({ active: true });
    const edge = container.querySelector('[data-testid="graph-edge-a-b"]')!;
    const path = edge.querySelector("path[fill='none']")!;
    expect(path.getAttribute("class")).toContain("stroke-graph-edge-active");
    const arrow = edge.querySelector("marker path")!;
    expect(arrow.getAttribute("class")).toContain("fill-graph-arrow-active");
  });

  it("renders distinct marker ids per edge instance", () => {
    const { container } = render(
      <svg>
        <GraphEdge source="a" target="b" points={POINTS} />
        <GraphEdge source="b" target="c" points={POINTS} />
      </svg>,
    );
    const markers = [...container.querySelectorAll("marker")];
    const ids = markers.map((m) => m.getAttribute("id"));
    expect(new Set(ids).size).toBe(2);
  });

  it("renders nothing with fewer than 2 points", () => {
    const { container } = renderEdge({ points: [{ x: 0, y: 0 }] });
    expect(container.querySelector("path")).toBeNull();
  });
});
