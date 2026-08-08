import { describe, it, expect, vi } from "vitest";
import { render, fireEvent, screen } from "@testing-library/react";
import {
  GraphNode,
  truncateLabel,
  ringSlotGeometry,
  NODE_TYPE_GLYPHS,
  type GraphNodeVisualStatus,
} from "./GraphNode";
import type { ParsedNode } from "../yaml/parseGraphSpec";

const RECT = { x: 100, y: 100, width: 140, height: 44 };
const FE0E = "\uFE0E";

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
) {
  return render(
    <svg>
      <GraphNode node={node} rect={RECT} {...props} />
    </svg>,
  );
}

describe("GraphNode", () => {
  it("renders glyph per node type with U+FE0E variation selector", () => {
    const cases: [string, string][] = [
      ["agent", "◉"],
      ["function", "ƒ"],
      ["delay", "◷"],
      ["human_input", "⏸"],
      ["graph", "⬕"],
    ];
    for (const [nodeType, glyph] of cases) {
      const { container, unmount } = renderNode(
        agentNode({ nodeType: nodeType as ParsedNode["nodeType"] }),
      );
      const el = container.querySelector('[data-testid="graph-node-designer"]');
      expect(el?.textContent).toContain(`${glyph}${FE0E}`);
      unmount();
    }
    // 映射表自身也全部携带 FE0E
    for (const g of Object.values(NODE_TYPE_GLYPHS)) {
      expect(g.endsWith(FE0E)).toBe(true);
    }
  });

  it("renders name (font-medium) and sub label (type · pool)", () => {
    const { container } = renderNode(agentNode());
    const texts = [...container.querySelectorAll("text")];
    const name = texts.find((el) => el.textContent === "designer");
    const sub = texts.find((el) => el.textContent === "agent · review");
    expect(name?.getAttribute("class")).toContain("font-medium");
    expect(name?.getAttribute("class")).toContain("text-ink");
    expect(sub?.getAttribute("class")).toContain("font-mono");
    expect(sub?.getAttribute("class")).toContain("text-faint");
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
    expect(truncateLabel("12345678901")).toBe("12345678901");
    expect(truncateLabel("123456789012")).toBe("1234567890…");
  });

  describe("status coloring matrix (§5.2 双通道)", () => {
    function bodyAndDot(status: GraphNodeVisualStatus) {
      const { container } = renderNode(agentNode(), { status });
      return {
        body: container.querySelector("[data-node-body]")!,
        dot: container.querySelector("[data-status-dot]")!,
        ring: container.querySelector("[data-ring-slot]"),
        unmount: container,
      };
    }

    it("pending: faint dot, hairline border, default fill", () => {
      const { body, dot, ring } = bodyAndDot("pending");
      expect(dot.getAttribute("class")).toContain("fill-faint");
      expect(body.getAttribute("class")).toContain("stroke-graph-node-border");
      expect(body.getAttribute("class")).toContain("fill-graph-node-fill");
      expect(ring).toBeNull();
    });

    it("running: hollow brand dot + brand border + ring slot reserved", () => {
      const { body, dot, ring } = bodyAndDot("running");
      expect(body.getAttribute("class")).toContain("stroke-brand");
      expect(dot.getAttribute("class")).toContain("stroke-brand");
      expect(dot.getAttribute("class")).toContain("fill-graph-node-fill");
      expect(ring).not.toBeNull();
      // 外扩 4px 同形圆角矩形(§4.4)
      expect(ring!.getAttribute("x")).toBe("-74");
      expect(ring!.getAttribute("y")).toBe("-26");
      expect(ring!.getAttribute("width")).toBe("148");
      expect(ring!.getAttribute("height")).toBe("52");
      expect(ring!.getAttribute("rx")).toBe("16");
    });

    it("completed: solid success dot + brand-soft body fill (双通道)", () => {
      const { body, dot, ring } = bodyAndDot("completed");
      expect(dot.getAttribute("class")).toContain("fill-success");
      expect(body.getAttribute("class")).toContain("fill-graph-node-fill-done");
      expect(ring).toBeNull();
    });

    it("crashed: danger dot + danger border", () => {
      const { body, dot } = bodyAndDot("crashed");
      expect(dot.getAttribute("class")).toContain("fill-danger");
      expect(body.getAttribute("class")).toContain("stroke-danger");
    });

    it("canceled: mute dot, hairline border", () => {
      const { body, dot } = bodyAndDot("canceled");
      expect(dot.getAttribute("class")).toContain("fill-mute");
      expect(body.getAttribute("class")).toContain("stroke-graph-node-border");
    });

    it("suspended: warning dot + warning dashed border", () => {
      const { body, dot } = bodyAndDot("suspended");
      expect(dot.getAttribute("class")).toContain("fill-warning");
      expect(body.getAttribute("class")).toContain("stroke-warning");
      expect(body.getAttribute("stroke-dasharray")).toBe("5 3");
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

  describe("virtual endpoints (统一形状)", () => {
    it("START renders as a unified rect with brand fill and centered 'START' label", () => {
      const { container } = renderNode(
        agentNode({ name: "__start__", nodeType: "__start__", config: {} }),
      );
      const body = container.querySelector("[data-node-body]")!;
      expect(body.getAttribute("class")).toContain("fill-brand");
      expect(body.getAttribute("width")).toBe("140");
      expect(body.getAttribute("height")).toBe("44");
      // 显示友好标签 "START"(不是 __start__)
      const texts = [...container.querySelectorAll("text")];
      const label = texts.find((el) => el.textContent === "START");
      expect(label).toBeDefined();
      expect(label?.getAttribute("text-anchor")).toBe("middle");
      expect(label?.getAttribute("class")).toContain("font-mono");
      expect(label?.getAttribute("class")).toContain("font-semibold");
      // <title> 保留原始内部名
      expect(container.querySelector("title")?.textContent).toBe("__start__");
      // 无 glyph、无 status dot
      expect(container.querySelector("[data-status-dot]")).toBeNull();
      // 不可交互(无 role=button)
      expect(container.querySelector('[role="button"]')).toBeNull();
    });

    it("END renders as a unified rect with brand fill and centered 'END' label", () => {
      const { container } = renderNode(
        agentNode({ name: "__end__", nodeType: "__end__", config: {} }),
      );
      const body = container.querySelector("[data-node-body]")!;
      expect(body.getAttribute("class")).toContain("fill-brand");
      expect(body.getAttribute("width")).toBe("140");
      expect(body.getAttribute("height")).toBe("44");
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
