import { describe, it, expect } from "vitest";
import { render } from "@testing-library/react";
import { GraphEdge, roundedPathD } from "./GraphEdge";

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

describe("roundedPathD", () => {
  it("keeps a straight two-point path as a plain line", () => {
    expect(roundedPathD([{ x: 0, y: 0 }, { x: 0, y: 100 }])).toBe(
      "M0 0 L0 100",
    );
  });

  it("rounds each interior corner with a quadratic curve (r=10)", () => {
    // Corner at (0,30): r clamps to 10 → enter at (0,20), exit at (8,36).
    expect(roundedPathD(POINTS)).toBe("M0 0 L0 20 Q0 30 8 36 L40 60");
  });

  it("clamps the radius to half the shorter adjacent segment", () => {
    // Incoming segment is only 4px long → r = 2.
    const pts = [
      { x: 0, y: 0 },
      { x: 0, y: 4 },
      { x: 40, y: 44 },
    ];
    expect(roundedPathD(pts, 10)).toBe("M0 0 L0 2 Q0 4 1.41 5.41 L40 44");
  });

  it("skips the curve for collinear points", () => {
    const pts = [
      { x: 0, y: 0 },
      { x: 0, y: 30 },
      { x: 0, y: 60 },
    ];
    expect(roundedPathD(pts)).toBe("M0 0 L0 30 L0 60");
  });

  it("returns an empty string for fewer than 2 points", () => {
    expect(roundedPathD([])).toBe("");
    expect(roundedPathD([{ x: 0, y: 0 }])).toBe("");
  });
});

describe("GraphEdge", () => {
  it("renders a 1.5px stroke with a same-color 7px arrow marker (§5.3)", () => {
    const { container } = renderEdge();
    const edge = container.querySelector('[data-testid="graph-edge-a-b"]')!;
    const path = edge.querySelector("path[fill='none']")!;
    expect(path.getAttribute("d")).toBe("M0 0 L0 20 Q0 30 8 36 L40 60");
    expect(path.getAttribute("stroke-width")).toBe("1.5");
    expect(path.getAttribute("class")).toContain("stroke-graph-edge");
    expect(path.getAttribute("marker-end")).toMatch(/^url\(#graph-arrow-/);
    const arrow = edge.querySelector("marker path")!;
    expect(arrow.getAttribute("class")).toContain("fill-graph-arrow");
    const marker = edge.querySelector("marker")!;
    expect(marker.getAttribute("markerWidth")).toBe("7");
    expect(marker.getAttribute("markerHeight")).toBe("7");
  });

  it("active state switches stroke and arrow to the active tokens at 2px", () => {
    const { container } = renderEdge({ active: true });
    const edge = container.querySelector('[data-testid="graph-edge-a-b"]')!;
    const path = edge.querySelector("path[fill='none']")!;
    expect(path.getAttribute("class")).toContain("stroke-graph-edge-active");
    expect(path.getAttribute("stroke-width")).toBe("2");
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
