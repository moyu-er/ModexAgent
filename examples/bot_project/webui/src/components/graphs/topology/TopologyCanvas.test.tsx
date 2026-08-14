import { describe, it, expect, vi, afterEach, beforeEach } from "vitest";
import { render, fireEvent, screen } from "@testing-library/react";
import {
  TopologyCanvas,
  clampZoom,
  zoomAt,
  panBy,
  MIN_ZOOM,
  MAX_ZOOM,
  type PulseSignal,
} from "./TopologyCanvas";
import { edgeKey } from "./layout";
import type { ParsedGraphTopology } from "../yaml/parseGraphSpec";

// PRD 附录 B 的 review_workflow(含 reviewer→implementer 回环边)。
const TOPOLOGY: ParsedGraphTopology = {
  name: "review_workflow",
  scheduler: "parallel",
  defaultTrigger: "on_all_preds",
  entryNode: "__start__",
  nodes: [
    { name: "__start__", nodeType: "__start__", config: {} },
    {
      name: "designer",
      nodeType: "agent",
      config: { agent: "designer", pool: "review" },
    },
    { name: "implementer", nodeType: "function", config: {} },
    { name: "reviewer", nodeType: "function", config: {} },
    { name: "__end__", nodeType: "__end__", config: {} },
  ],
  edges: [
    { source: "__start__", target: "designer" },
    { source: "designer", target: "implementer" },
    { source: "implementer", target: "reviewer" },
    { source: "reviewer", target: "implementer" },
    { source: "reviewer", target: "__end__" },
  ],
};

function renderCanvas(
  props: Partial<Parameters<typeof TopologyCanvas>[0]> = {},
) {
  return render(<TopologyCanvas topology={TOPOLOGY} {...props} />);
}

describe("TopologyCanvas", () => {
  it("renders all nodes and edges of the topology (含回环边)", () => {
    renderCanvas();
    for (const name of [
      "__start__",
      "designer",
      "implementer",
      "reviewer",
      "__end__",
    ]) {
      expect(screen.getByTestId(`graph-node-${name}`)).toBeTruthy();
    }
    for (const e of TOPOLOGY.edges) {
      expect(
        screen.getByTestId(`graph-edge-${edgeKey(e.source, e.target)}`),
      ).toBeTruthy();
    }
  });

  it("renders the colored 6-status legend chips (PRD §6.3, i18n legend keys)", () => {
    renderCanvas();
    const legend = screen.getByTestId("graph-canvas-legend");
    expect(legend.getAttribute("class")).toContain("text-xs");
    expect(legend.getAttribute("class")).toContain("text-body");
    const chips = legend.querySelectorAll("[data-legend-status]");
    expect(chips).toHaveLength(6);
    const dotOf = (status: string) =>
      legend.querySelector(`[data-legend-status="${status}"] [data-legend-dot]`)!;
    expect(dotOf("pending").getAttribute("class")).toContain(
      "bg-graph-status-pending",
    );
    expect(dotOf("running").getAttribute("class")).toContain(
      "bg-graph-status-running",
    );
    expect(dotOf("completed").getAttribute("class")).toContain(
      "bg-graph-status-completed",
    );
    expect(dotOf("suspended").getAttribute("class")).toContain(
      "bg-graph-status-suspended",
    );
    expect(dotOf("canceled").getAttribute("class")).toContain(
      "bg-graph-status-canceled",
    );
    // crashed 用 ✕ 字形而非圆点
    const crashed = legend.querySelector('[data-legend-status="crashed"]')!;
    expect(crashed.textContent).toContain("✕");
    expect(crashed.querySelector("[data-legend-dot]")).toBeNull();
    // 六个状态标签全部走 i18n
    for (const label of [
      "pending",
      "running",
      "completed",
      "crashed",
      "suspended",
      "canceled",
    ]) {
      expect(legend.textContent).toContain(label);
    }
  });

  it("passes node statuses through to nodes", () => {
    renderCanvas({ nodeStatuses: { designer: "running" } });
    const node = screen.getByTestId("graph-node-designer");
    expect(node.getAttribute("data-status")).toBe("running");
    expect(node.querySelector("[data-ring-slot]")).not.toBeNull();
    expect(
      screen.getByTestId("graph-node-implementer").getAttribute("data-status"),
    ).toBe("pending");
  });

  it("marks activeEdges with the active stroke token", () => {
    renderCanvas({
      activeEdges: new Set([edgeKey("designer", "implementer")]),
    });
    const active = screen
      .getByTestId("graph-edge-designer-implementer")
      .querySelector("path[fill='none']")!;
    expect(active.getAttribute("class")).toContain("stroke-graph-edge-active");
    const idle = screen
      .getByTestId("graph-edge-implementer-reviewer")
      .querySelector("path[fill='none']")!;
    expect(idle.getAttribute("class")).toContain("stroke-graph-edge");
  });

  describe("selection (受控)", () => {
    it("click on a non-agent node fires onSelectNode with the node name", () => {
      const onSelectNode = vi.fn();
      renderCanvas({ onSelectNode });
      fireEvent.click(screen.getByRole("button", { name: "implementer" }));
      expect(onSelectNode).toHaveBeenCalledWith("implementer");
    });

    it("Enter on a focused node fires onSelectNode", () => {
      const onSelectNode = vi.fn();
      renderCanvas({ onSelectNode });
      fireEvent.keyDown(screen.getByRole("button", { name: "reviewer" }), {
        key: "Enter",
      });
      expect(onSelectNode).toHaveBeenCalledWith("reviewer");
    });

    it("selectedNodeId drives the selected highlight", () => {
      renderCanvas({ selectedNodeId: "implementer", onSelectNode: () => {} });
      const body = screen
        .getByTestId("graph-node-implementer")
        .querySelector("[data-node-body]")!;
      expect(body.getAttribute("class")).toContain(
        "stroke-graph-node-border-active",
      );
    });

    it("single click on an agent node fires onOpenSession", () => {
      const onOpenSession = vi.fn();
      renderCanvas({ onOpenSession });
      fireEvent.click(screen.getByRole("button", { name: "designer" }));
      expect(onOpenSession).toHaveBeenCalledWith("designer");
    });
  });

  describe("zoom / pan (纯函数,happy-dom 不测指针拖拽)", () => {
    it("clampZoom bounds the scale to 0.5x–2x", () => {
      expect(clampZoom(0.1)).toBe(MIN_ZOOM);
      expect(clampZoom(1)).toBe(1);
      expect(clampZoom(99)).toBe(MAX_ZOOM);
    });

    it("zoomAt keeps the anchor point fixed on screen", () => {
      const t = { scale: 1, tx: 0, ty: 0 };
      const next = zoomAt(t, 100, 50, 2);
      expect(next.scale).toBe(2);
      // 不动点:缩放前后 u*scale+t 相等
      expect(100 * next.scale + next.tx).toBeCloseTo(100 * t.scale + t.tx);
      expect(50 * next.scale + next.ty).toBeCloseTo(50 * t.scale + t.ty);
    });

    it("zoomAt clamps instead of overshooting the bounds", () => {
      const next = zoomAt({ scale: 1, tx: 0, ty: 0 }, 0, 0, 10);
      expect(next.scale).toBe(MAX_ZOOM);
    });

    it("panBy translates the view", () => {
      expect(panBy({ scale: 1, tx: 5, ty: 6 }, 10, -4)).toEqual({
        scale: 1,
        tx: 15,
        ty: 2,
      });
    });

    it("wheel events zoom the viewport transform", () => {
      const { container } = renderCanvas();
      const svg = container.querySelector("svg")!;
      const viewport = screen.getByTestId("topology-viewport");
      expect(viewport.getAttribute("transform")).toContain("scale(1)");
      fireEvent.wheel(svg, { deltaY: -120 });
      expect(viewport.getAttribute("transform")).not.toContain("scale(1)");
    });
  });

  // ── G03: ActiveNodeRing 集成 ──────────────────────────────────

  describe("ActiveNodeRing integration (§4.4)", () => {
    it("renders an active-node-ring for each running node", () => {
      renderCanvas({ nodeStatuses: { designer: "running", reviewer: "running" } });
      const rings = screen.getAllByTestId("active-node-ring");
      expect(rings).toHaveLength(2);
    });

    it("does not render rings for non-running nodes", () => {
      renderCanvas({
        nodeStatuses: { designer: "completed", implementer: "pending" },
      });
      expect(screen.queryAllByTestId("active-node-ring")).toHaveLength(0);
    });

    it("ring geometry matches the node's layout rect (外扩 4px)", () => {
      renderCanvas({ nodeStatuses: { designer: "running" } });
      const ring = screen.getByTestId("active-node-ring");
      // Functional node: 140×44 → ring 148×52, rx 16
      expect(ring.getAttribute("width")).toBe("148");
      expect(ring.getAttribute("height")).toBe("52");
      expect(ring.getAttribute("rx")).toBe("16");
      expect(ring.getAttribute("class")).toContain("graph-ring-pulse");
    });
  });

  // ── Crash-flash outline (§8.1) ─────────────────────────────────

  describe("crash flash outline (§8.1)", () => {
    it("renders a red flash outline for each node named in crashNodeNames", () => {
      renderCanvas({ crashNodeNames: new Set(["implementer"]) });
      const viewport = screen.getByTestId("topology-viewport");
      const flashes = viewport.querySelectorAll("[data-crash-flash]");
      expect(flashes).toHaveLength(1);
      const flash = flashes[0]!;
      // Geometry mirrors ringSlotGeometry: 140×44 node → 148×52 outset rect, rx 16.
      expect(flash.getAttribute("width")).toBe("148");
      expect(flash.getAttribute("height")).toBe("52");
      expect(flash.getAttribute("rx")).toBe("16");
      expect(flash.getAttribute("class")).toContain("stroke-graph-status-crashed");
      expect(flash.getAttribute("fill")).toBe("none");
      expect(flash.getAttribute("pointer-events")).toBe("none");
    });

    it("renders no flash outline when crashNodeNames is empty or absent", () => {
      const { unmount } = renderCanvas({ crashNodeNames: new Set() });
      expect(
        screen
          .getByTestId("topology-viewport")
          .querySelectorAll("[data-crash-flash]"),
      ).toHaveLength(0);
      unmount();
      renderCanvas();
      expect(
        screen
          .getByTestId("topology-viewport")
          .querySelectorAll("[data-crash-flash]"),
      ).toHaveLength(0);
    });
  });

  // ── G03: DeliverPulse 集成 ────────────────────────────────────

  describe("DeliverPulse integration (§4.3)", () => {
    const PULSE: PulseSignal = {
      id: 1,
      edgeKey: edgeKey("designer", "implementer"),
      points: [
        { x: 0, y: 0 },
        { x: 100, y: 0 },
      ],
    };

    // happy-dom 默认无 matchMedia → DeliverPulse 走正常模式(rAF)
    // 需要 mock matchMedia 确保行为可预测。
    function mockMatchMedia(reduce: boolean): void {
      const mm = vi.fn().mockImplementation((query: string) => ({
        matches: reduce && query.includes("reduce"),
        media: query,
        onchange: null,
        addListener: vi.fn(),
        removeListener: vi.fn(),
        addEventListener: vi.fn(),
        removeEventListener: vi.fn(),
        dispatchEvent: vi.fn(),
      }));
      vi.stubGlobal("matchMedia", mm);
    }

    beforeEach(() => {
      mockMatchMedia(false);
    });

    afterEach(() => {
      vi.unstubAllGlobals();
      vi.useRealTimers();
    });

    /** Bridge requestAnimationFrame → setTimeout so vitest fake timers can drive it. */
    function mockRaf(): void {
      let frame = 0;
      vi.stubGlobal("requestAnimationFrame", (cb: (t: number) => void) => {
        frame += 16;
        return setTimeout(() => cb(frame), 0) as unknown as number;
      });
      vi.stubGlobal("cancelAnimationFrame", (id: number) => {
        clearTimeout(id);
      });
    }

    it("renders a DeliverPulse for each entry in pulses", () => {
      renderCanvas({
        pulses: [
          PULSE,
          { id: 2, edgeKey: "x-y", points: PULSE.points },
        ],
      });
      const pulses = screen.getAllByTestId("deliver-pulse");
      expect(pulses).toHaveLength(2);
    });

    it("renders no pulse when pulses is empty or undefined", () => {
      renderCanvas();
      expect(screen.queryAllByTestId("deliver-pulse")).toHaveLength(0);
      expect(screen.queryAllByTestId("deliver-pulse-fallback")).toHaveLength(0);
    });

    it("calls onPulseComplete after the pulse duration", () => {
      vi.useFakeTimers();
      mockRaf();
      const onPulseComplete = vi.fn();
      renderCanvas({ pulses: [PULSE], onPulseComplete });

      vi.advanceTimersByTime(700);
      expect(onPulseComplete).toHaveBeenCalledWith(PULSE.id);
    });
  });
});
