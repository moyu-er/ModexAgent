import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { MiniTopology } from "./MiniTopology";
import { computeMiniLayout, MINI_FOLD_THRESHOLD } from "./miniLayout";
import type { ParsedGraphTopology, ParsedNode } from "../yaml/parseGraphSpec";

function makeTopology(functionalNames: string[]): ParsedGraphTopology {
  const nodes: ParsedNode[] = [
    { name: "__start__", nodeType: "__start__", config: {} },
    ...functionalNames.map((name) => ({
      name,
      nodeType: "agent" as const,
      config: {},
    })),
    { name: "__end__", nodeType: "__end__", config: {} },
  ];
  const chain = ["__start__", ...functionalNames, "__end__"];
  const edges = chain.slice(0, -1).map((source, i) => ({
    source,
    target: chain[i + 1]!,
  }));
  return {
    name: "wf",
    scheduler: "linear",
    defaultTrigger: "on_all_preds",
    nodes,
    edges,
    entryNode: "__start__",
  };
}

const SMALL = makeTopology(["a", "b", "c"]); // 5 nodes
const LARGE = makeTopology(["n1", "n2", "n3", "n4", "n5", "n6", "n7", "n8"]); // 10 nodes

describe("MiniTopology", () => {
  it("renders a fixed 80×24 svg with no text and no interaction", () => {
    const { container } = render(<MiniTopology topology={SMALL} />);
    const svg = screen.getByTestId("mini-topology");
    expect(svg.getAttribute("width")).toBe("80");
    expect(svg.getAttribute("height")).toBe("24");
    expect(svg.textContent).toBe("");
    expect(container.querySelector("text")).toBeNull();
  });

  it("≤8 nodes: every node rendered as a unified circle", () => {
    const { container } = render(<MiniTopology topology={SMALL} />);
    expect(container.querySelectorAll("circle")).toHaveLength(5);
    expect(container.querySelectorAll("rect")).toHaveLength(0);
    expect(container.querySelectorAll("line")).toHaveLength(4);
    expect(container.querySelectorAll("[data-fold-dot]")).toHaveLength(0);
    expect(screen.getByTestId("mini-topology").getAttribute("data-folded")).toBe(
      "false",
    );
    // START/END 用 brand 色
    const start = container.querySelector('[data-mini-node="__start__"]')!;
    expect(start.getAttribute("class")).toContain("fill-graph-mini-start");
    const end = container.querySelector('[data-mini-node="__end__"]')!;
    expect(end.getAttribute("class")).toContain("fill-graph-mini-end");
  });

  it(">8 nodes: folds the middle chain into a ··· tick", () => {
    const { container } = render(<MiniTopology topology={LARGE} />);
    expect(screen.getByTestId("mini-topology").getAttribute("data-folded")).toBe(
      "true",
    );
    // 保留 START/END + 首末功能节点,中间折叠为三点刻度
    const kept = [...container.querySelectorAll("[data-mini-node]")].map((el) =>
      el.getAttribute("data-mini-node"),
    );
    expect(kept).toEqual(["__start__", "n1", "n8", "__end__"]);
    expect(container.querySelectorAll("[data-fold-dot]")).toHaveLength(3);
  });

  it("applies status coloring when nodeStatuses are given (实例列表)", () => {
    const { container } = render(
      <MiniTopology topology={SMALL} nodeStatuses={{ b: "running" }} />,
    );
    const running = container.querySelector('[data-mini-node="b"]')!;
    expect(running.getAttribute("class")).toContain("fill-graph-status-running");
    const idle = container.querySelector('[data-mini-node="a"]')!;
    expect(idle.getAttribute("class")).toContain("fill-graph-mini-node");
  });

  it("syncs every status to the graph-status palette (§6.5)", () => {
    const { container } = render(
      <MiniTopology
        topology={SMALL}
        nodeStatuses={{ a: "completed", b: "crashed", c: "suspended" }}
      />,
    );
    const fillOf = (name: string) =>
      container.querySelector(`[data-mini-node="${name}"]`)!.getAttribute("class")!;
    expect(fillOf("a")).toContain("fill-graph-status-completed");
    expect(fillOf("b")).toContain("fill-graph-status-crashed");
    expect(fillOf("c")).toContain("fill-graph-status-suspended");
  });
});

describe("computeMiniLayout", () => {
  it("keeps everything inside the 80×24 box for small graphs", () => {
    const layout = computeMiniLayout(SMALL);
    expect(layout.foldDots).toBeNull();
    for (const node of layout.nodes) {
      expect(node.x).toBeGreaterThanOrEqual(0);
      expect(node.x).toBeLessThanOrEqual(80);
      expect(node.y).toBeGreaterThanOrEqual(0);
      expect(node.y).toBeLessThanOrEqual(24);
    }
    expect(layout.segments.length).toBe(SMALL.edges.length);
  });

  it("folds exactly when the node count exceeds the threshold", () => {
    const atThreshold = makeTopology(
      Array.from({ length: MINI_FOLD_THRESHOLD - 2 }, (_, i) => `f${i}`),
    ); // 6 functional + start + end = 8
    expect(computeMiniLayout(atThreshold).foldDots).toBeNull();
    const over = makeTopology(
      Array.from({ length: MINI_FOLD_THRESHOLD - 1 }, (_, i) => `f${i}`),
    ); // 9 nodes
    const folded = computeMiniLayout(over);
    expect(folded.foldDots).toHaveLength(3);
    expect(folded.nodes.map((n) => n.name)).toEqual([
      "__start__",
      "f0",
      "f6",
      "__end__",
    ]);
    // 折叠段串联:start→f0→fold→f6→end
    expect(folded.segments.length).toBe(4);
  });

  it("handles an empty topology", () => {
    const layout = computeMiniLayout({
      name: "empty",
      scheduler: "linear",
      defaultTrigger: "on_all_preds",
      nodes: [],
      edges: [],
      entryNode: "__start__",
    });
    expect(layout.nodes).toEqual([]);
    expect(layout.segments).toEqual([]);
  });
});
