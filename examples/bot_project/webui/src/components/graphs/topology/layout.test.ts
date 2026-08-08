/**
 * layoutGraph 单元测试(graph PRD §5.4/§9.2 / tickets G01)。
 *
 * 用真实 review_workflow spec 的解析结果做布局:验证 TB 方向、节点不重叠、
 * 坐标为有限数、回环边(reviewer→implementer)产出点路径。
 */
import { describe, expect, it } from "vitest";

import { parseGraphSpecYaml } from "../yaml/parseGraphSpec";
import { edgeKey, layoutGraph, type LayoutNodeRect } from "./layout";

const REVIEW_WORKFLOW_YML = `name: review_workflow
version: "1.0"
state_class: default
scheduler: parallel
max_iterations: 50
default_trigger: on_receive
nodes:
  - name: designer
    node_type: agent
    config:
      agent: designer
      pool: review
    trigger: on_receive
  - name: implementer
    node_type: agent
    config:
      agent: implementer
      pool: review
    trigger: on_receive
  - name: reviewer
    node_type: agent
    config:
      agent: reviewer
      pool: review
    trigger: on_receive
edges:
  - source: __start__
    target: designer
  - source: designer
    target: implementer
  - source: implementer
    target: reviewer
  - source: reviewer
    target: implementer
  - source: reviewer
    target: __end__
`;

const SIMPLE_YML = `name: simple
version: "1.0"
state_class: default
nodes: []
edges:
  - source: __start__
    target: __end__
`;

function bounds(rect: LayoutNodeRect): {
  left: number;
  right: number;
  top: number;
  bottom: number;
} {
  return {
    left: rect.x - rect.width / 2,
    right: rect.x + rect.width / 2,
    top: rect.y - rect.height / 2,
    bottom: rect.y + rect.height / 2,
  };
}

function overlaps(a: LayoutNodeRect, b: LayoutNodeRect): boolean {
  const ab = bounds(a);
  const bb = bounds(b);
  return ab.left < bb.right && ab.right > bb.left && ab.top < bb.bottom && ab.bottom > bb.top;
}

describe("layoutGraph", () => {
  it("review_workflow:全部 5 节点有有限坐标与声明尺寸", () => {
    const topo = parseGraphSpecYaml(REVIEW_WORKFLOW_YML);
    const layout = layoutGraph(topo);

    expect([...layout.nodes.keys()].sort()).toEqual(
      ["__end__", "__start__", "designer", "implementer", "reviewer"].sort(),
    );
    for (const [, rect] of layout.nodes) {
      expect(Number.isFinite(rect.x)).toBe(true);
      expect(Number.isFinite(rect.y)).toBe(true);
      expect(rect.width).toBeGreaterThan(0);
      expect(rect.height).toBeGreaterThan(0);
    }
    // 尺寸规范:所有节点统一 140×44(含 START/END — Rev 3 统一形状)
    expect(layout.nodes.get("designer")).toMatchObject({ width: 140, height: 44 });
    expect(layout.nodes.get("__start__")).toMatchObject({ width: 140, height: 44 });
    expect(layout.nodes.get("__end__")).toMatchObject({ width: 140, height: 44 });
  });

  it("review_workflow:TB 方向 — y 坐标沿 start→…→end 递增", () => {
    const layout = layoutGraph(parseGraphSpecYaml(REVIEW_WORKFLOW_YML));
    const y = (name: string): number => {
      const rect = layout.nodes.get(name);
      if (!rect) throw new Error(`node ${name} missing`);
      return rect.y;
    };

    expect(y("__start__")).toBeLessThan(y("designer"));
    expect(y("designer")).toBeLessThan(y("implementer"));
    expect(y("implementer")).toBeLessThan(y("reviewer"));
    expect(y("reviewer")).toBeLessThan(y("__end__"));
  });

  it("review_workflow:节点两两不重叠", () => {
    const layout = layoutGraph(parseGraphSpecYaml(REVIEW_WORKFLOW_YML));
    const rects = [...layout.nodes.values()];
    for (let i = 0; i < rects.length; i += 1) {
      for (let j = i + 1; j < rects.length; j += 1) {
        const a = rects[i];
        const b = rects[j];
        if (a && b) {
          expect(overlaps(a, b)).toBe(false);
        }
      }
    }
  });

  it("review_workflow:5 条边全部有点路径,回环边存在", () => {
    const layout = layoutGraph(parseGraphSpecYaml(REVIEW_WORKFLOW_YML));

    expect(layout.edges.size).toBe(5);
    for (const [key, edge] of layout.edges) {
      expect(edge.points.length, `edge ${key}`).toBeGreaterThanOrEqual(2);
      for (const p of edge.points) {
        expect(Number.isFinite(p.x)).toBe(true);
        expect(Number.isFinite(p.y)).toBe(true);
      }
    }
    // 回环边 reviewer→implementer:dagre 反向路由,点多于直线两端
    const loop = layout.edges.get(edgeKey("reviewer", "implementer"));
    expect(loop).toBeDefined();
    expect(loop && loop.points.length).toBeGreaterThanOrEqual(2);
    // 回环方向:从 reviewer(下)回到 implementer(上)
    const first = loop?.points[0];
    const last = loop?.points[loop.points.length - 1];
    if (first && last) {
      expect(first.y).toBeGreaterThan(last.y);
    }
  });

  it("simple:start→end 直连,坐标有限", () => {
    const layout = layoutGraph(parseGraphSpecYaml(SIMPLE_YML));
    expect(layout.nodes.size).toBe(2);
    const edge = layout.edges.get(edgeKey("__start__", "__end__"));
    expect(edge).toBeDefined();
    expect(edge && edge.points.length).toBeGreaterThanOrEqual(2);
  });
});
