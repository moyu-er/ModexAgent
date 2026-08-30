import { describe, it, expect, vi } from "vitest";
import { render, fireEvent, screen } from "@testing-library/react";
import {
  GraphNode,
  truncateLabel,
  ringSlotGeometry,
  NODE_TYPE_ICONS,
  type GraphNodeVisualStatus,
} from "./GraphNode";
import type { ParsedNode } from "../yaml/parseGraphSpec";

const RECT = { x: 100, y: 100, width: 140, height: 44 };
const ENDPOINT_RECT = { x: 100, y: 100, width: 76, height: 30 };

function agentNode(overrides: Partial<ParsedNode> = {}): ParsedNode {
  return {
    name: "designer",
    nodeType: "agent",
    config: { agent: "designer", pool: "review" },
    ...overrides,
  };
}

function renderNode(
  node: ParsedNode,
  props: Partial<Parameters<typeof GraphNode>[0]> = {},
  rect = RECT,
) {
  return render(
    <svg>
      <GraphNode node={node} rect={rect} {...props} />
    </svg>,
  );
}

describe("GraphNode", () => {
  it("renders a lucide SVG icon per node type", () => {
    const cases: [string, string][] = [
      ["agent", "bot"],
      ["function", "braces"],
      ["delay", "timer"],
      ["human_input", "user"],
      ["graph", "workflow"],
      // Scope 声明层级(票据16)
      ["workspace", "layers"],
      ["pool", "boxes"],
    ];
    for (const [nodeType, iconName] of cases) {
      const { container, unmount } = renderNode(
        agentNode({ nodeType: nodeType as ParsedNode["nodeType"] }),
      );
      const el = container.querySelector('[data-testid="graph-node-designer"]');
      const icon = el?.querySelector(`svg.lucide-${iconName}`);
      expect(icon).not.toBeNull();
      expect(icon?.getAttribute("width")).toBe("14");
      unmount();
    }
    // 映射表自身与五种功能类型 + 两个 scope 层级一一对应
    expect(Object.keys(NODE_TYPE_ICONS).sort()).toEqual(
      ["agent", "delay", "function", "graph", "human_input", "pool", "workspace"].sort(),
    );
  });

  it("renders name (font-medium) and sub label (type · pool, text-mute)", () => {
    const { container } = renderNode(agentNode());
    const texts = [...container.querySelectorAll("text")];
    const name = texts.find((el) => el.textContent === "designer");
    const sub = texts.find((el) => el.textContent === "agent · review");
    expect(name?.getAttribute("class")).toContain("font-medium");
    expect(name?.getAttribute("class")).toContain("text-ink");
    expect(sub?.getAttribute("class")).toContain("font-mono");
    expect(sub?.getAttribute("class")).toContain("text-mute");
  });

  it("truncates long names with ellipsis and keeps the full name in <title>", () => {
    const long = "a-very-long-node-name";
    const { container } = renderNode(agentNode({ name: long }));
    const el = container.querySelector(`[data-testid="graph-node-${long}"]`);
    expect(el?.querySelector("title")?.textContent).toBe(long);
    const visible = [...el!.querySelectorAll("text")].map((t) => t.textContent);
    expect(visible).toContain(truncateLabel(long));
    expect(truncateLabel(long).endsWith("…")).toBe(true);
    expect(visible).not.toContain(long);
  });

  it("truncateLabel leaves short text untouched", () => {
    expect(truncateLabel("abc")).toBe("abc");
    expect(truncateLabel("123456789012")).toBe("123456789012");
    expect(truncateLabel("1234567890123")).toBe("12345678901…");
  });

  describe("status coloring (§6 Rev 4 dot-only — 节点本体不随状态变化)", () => {
    function bodyAndDot(status: GraphNodeVisualStatus) {
      const { container } = renderNode(agentNode(), { status });
      return {
        body: container.querySelector("[data-node-body]")!,
        dot: container.querySelector("[data-status-dot]")!,
        ring: container.querySelector("[data-ring-slot]"),
        unmount: container,
      };
    }

    it.each([
      ["pending", "fill-graph-status-pending"],
      ["running", "fill-graph-status-running"],
      ["completed", "fill-graph-status-completed"],
      ["crashed", "fill-graph-status-crashed"],
      ["suspended", "fill-graph-status-suspended"],
      ["canceled", "fill-graph-status-canceled"],
    ] as const)(" %s: solid status dot, body stays neutral", (status, dotCls) => {
      const { body, dot } = bodyAndDot(status);
      expect(dot.getAttribute("class")).toContain(dotCls);
      expect(dot.getAttribute("r")).toBe("5");
      expect(body.getAttribute("class")).toContain("fill-graph-node-fill");
      expect(body.getAttribute("class")).toContain("stroke-graph-node-border");
      expect(body.getAttribute("stroke-dasharray")).toBeNull();
    });

    it("running: ring slot reserved (motion cue outside the node body)", () => {
      const { ring } = bodyAndDot("running");
      expect(ring).not.toBeNull();
      // 外扩 4px 同形圆角矩形(§4.4)
      expect(ring!.getAttribute("x")).toBe("-74");
      expect(ring!.getAttribute("y")).toBe("-26");
      expect(ring!.getAttribute("width")).toBe("148");
      expect(ring!.getAttribute("height")).toBe("52");
      expect(ring!.getAttribute("rx")).toBe("16");
    });

    it("non-running statuses render no ring slot", () => {
      const { ring } = bodyAndDot("completed");
      expect(ring).toBeNull();
    });

    it("canceled: no strikethrough — the violet dot is the only channel", () => {
      const { container } = renderNode(agentNode(), { status: "canceled" });
      const name = [...container.querySelectorAll("text")].find(
        (el) => el.textContent === "designer",
      )!;
      expect(name.getAttribute("class")).not.toContain("line-through");
    });

    it("all six dot classes are mutually distinct", () => {
      const classes = (
        [
          "pending",
          "running",
          "completed",
          "crashed",
          "suspended",
          "canceled",
        ] as const
      ).map((s) => bodyAndDot(s).dot.getAttribute("class")!);
      expect(new Set(classes).size).toBe(6);
    });
  });

  it("selected state uses the active border token (§8.1 选中高亮)", () => {
    const { container } = renderNode(
      agentNode({ nodeType: "function" }),
      { selected: true },
    );
    const body = container.querySelector("[data-node-body]")!;
    expect(body.getAttribute("class")).toContain(
      "stroke-graph-node-border-active",
    );
    expect(body.getAttribute("stroke-width")).toBe("2");
  });

  it("ringSlotGeometry outsets the node rect by 4px with radius + 4", () => {
    expect(ringSlotGeometry(140, 44)).toEqual({
      x: -74,
      y: -26,
      width: 148,
      height: 52,
      rx: 16,
    });
  });

  describe("virtual endpoints (Rev 4 ghost pill)", () => {
    it("START renders as a ghost pill with centered 'START' label", () => {
      const { container } = renderNode(
        agentNode({ name: "__start__", nodeType: "__start__", config: {} }),
        {},
        ENDPOINT_RECT,
      );
      const body = container.querySelector("[data-node-body]")!;
      expect(body.getAttribute("class")).toContain("fill-graph-endpoint-fill");
      expect(body.getAttribute("class")).toContain("stroke-graph-endpoint-border");
      expect(body.getAttribute("width")).toBe("76");
      expect(body.getAttribute("height")).toBe("30");
      // 全圆角药丸:rx = height / 2
      expect(body.getAttribute("rx")).toBe("15");
      // 显示友好标签 "START"(不是 __start__)
      const texts = [...container.querySelectorAll("text")];
      const label = texts.find((el) => el.textContent === "START");
      expect(label).toBeDefined();
      expect(label?.getAttribute("text-anchor")).toBe("middle");
      expect(label?.getAttribute("class")).toContain("font-mono");
      expect(label?.getAttribute("class")).toContain("font-semibold");
      expect(label?.getAttribute("class")).toContain("text-graph-endpoint-text");
      // <title> 保留原始内部名
      expect(container.querySelector("title")?.textContent).toBe("__start__");
      // 无图标、无 status dot
      expect(container.querySelector("[data-status-dot]")).toBeNull();
      expect(container.querySelector("svg.lucide")).toBeNull();
      // 不可交互(无 role=button)
      expect(container.querySelector('[role="button"]')).toBeNull();
    });

    it("END renders as a ghost pill with centered 'END' label", () => {
      const { container } = renderNode(
        agentNode({ name: "__end__", nodeType: "__end__", config: {} }),
        {},
        ENDPOINT_RECT,
      );
      const body = container.querySelector("[data-node-body]")!;
      expect(body.getAttribute("class")).toContain("fill-graph-endpoint-fill");
      const texts = [...container.querySelectorAll("text")];
      const label = texts.find((el) => el.textContent === "END");
      expect(label).toBeDefined();
      expect(label?.getAttribute("text-anchor")).toBe("middle");
      expect(container.querySelector("title")?.textContent).toBe("__end__");
      expect(container.querySelector("[data-status-dot]")).toBeNull();
      expect(container.querySelector('[role="button"]')).toBeNull();
    });
  });

  describe("interaction", () => {
    it("agent node: single click fires onOpen (jump to session)", () => {
      const onOpen = vi.fn();
      renderNode(agentNode(), { onOpen });
      const el = screen.getByRole("button", { name: "designer" });
      expect(el.getAttribute("tabindex")).toBe("0");
      fireEvent.click(el);
      expect(onOpen).toHaveBeenCalledWith("designer");
    });

    it("agent node: Enter fires onOpen", () => {
      const onOpen = vi.fn();
      renderNode(agentNode(), { onOpen });
      const el = screen.getByRole("button", { name: "designer" });
      fireEvent.keyDown(el, { key: "Enter" });
      expect(onOpen).toHaveBeenCalledWith("designer");
    });

    it("non-agent node: click fires onSelect (not onOpen)", () => {
      const onSelect = vi.fn();
      const onOpen = vi.fn();
      renderNode(agentNode({ nodeType: "function" }), { onSelect, onOpen });
      const el = screen.getByRole("button", { name: "designer" });
      fireEvent.click(el);
      expect(onSelect).toHaveBeenCalledWith("designer");
      expect(onOpen).not.toHaveBeenCalled();
    });

    it("non-agent node: Enter fires onSelect", () => {
      const onSelect = vi.fn();
      renderNode(agentNode({ nodeType: "function" }), { onSelect });
      const el = screen.getByRole("button", { name: "designer" });
      fireEvent.keyDown(el, { key: "Enter" });
      expect(onSelect).toHaveBeenCalledWith("designer");
      fireEvent.keyDown(el, { key: " " });
      expect(onSelect).toHaveBeenCalledTimes(1);
    });

    it("focus shows the brand focus ring for non-agent nodes", () => {
      const { container } = renderNode(
        agentNode({ nodeType: "function" }),
        { onSelect: () => {} },
      );
      const el = screen.getByRole("button", { name: "designer" });
      const body = container.querySelector("[data-node-body]")!;
      expect(body.getAttribute("class")).not.toContain(
        "stroke-graph-node-border-active",
      );
      fireEvent.focus(el);
      expect(body.getAttribute("class")).toContain(
        "stroke-graph-node-border-active",
      );
      fireEvent.blur(el);
      expect(body.getAttribute("class")).not.toContain(
        "stroke-graph-node-border-active",
      );
    });
  });
});
