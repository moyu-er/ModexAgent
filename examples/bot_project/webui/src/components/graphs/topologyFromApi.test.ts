// topologyFromApi 单元测试 — 拓扑端点 DTO(§11.3)→ 渲染模型的映射契约。
// DTO fixture 与后端 graph_routes.handle_get_topology 的序列化形状一致
// (NodeTopologyInfo 携带 trigger: null,config 为自由 dict)。
import { describe, expect, it } from "vitest";

import type { GraphTopology } from "../../lib/graphsApi";
import { topologyFromApi } from "./topologyFromApi";

const REVIEW_DTO: GraphTopology = {
  spec_id: "42",
  name: "review_workflow",
  scheduler: "parallel",
  default_trigger: "on_all_preds",
  nodes: [
    {
      name: "designer",
      node_type: "agent",
      config: { agent: "designer", pool: "review", description: "ignored" },
      trigger: null,
    },
    { name: "gate", node_type: "human_input", config: {}, trigger: "on_all_preds" },
  ],
  edges: [
    { source: "__start__", target: "designer" },
    { source: "designer", target: "gate" },
    { source: "gate", target: "__end__" },
  ],
  entry_node: "__start__",
};

describe("topologyFromApi", () => {
  it("maps nodes/edges and synthesizes boundary endpoints", () => {
    const topo = topologyFromApi(REVIEW_DTO);

    expect(topo.name).toBe("review_workflow");
    expect(topo.scheduler).toBe("parallel");
    expect(topo.defaultTrigger).toBe("on_all_preds");
    expect(topo.entryNode).toBe("__start__");
    // 与 parseGraphSpecYaml 相同的节点顺序契约:__start__ 置顶、__end__ 置尾
    expect(topo.nodes.map((n) => n.name)).toEqual([
      "__start__",
      "designer",
      "gate",
      "__end__",
    ]);
    expect(topo.nodes[1]).toEqual({
      name: "designer",
      nodeType: "agent",
      // config 只提取渲染用的 agent/pool,其余键忽略
      config: { agent: "designer", pool: "review" },
    });
    expect(topo.nodes[2]).toEqual({
      name: "gate",
      nodeType: "human_input",
      config: {},
      trigger: "on_all_preds",
    });
    expect(topo.edges).toEqual(REVIEW_DTO.edges);
  });

  it("does not synthesize unreferenced boundary endpoints", () => {
    const topo = topologyFromApi({
      ...REVIEW_DTO,
      edges: [{ source: "__start__", target: "designer" }],
    });
    expect(topo.nodes.map((n) => n.name)).toEqual(["__start__", "designer", "gate"]);
  });

  it("throws on unknown scheduler", () => {
    expect(() =>
      topologyFromApi({ ...REVIEW_DTO, scheduler: "roundrobin" }),
    ).toThrowError(/unknown scheduler 'roundrobin'/);
  });

  it("throws on unknown default_trigger", () => {
    expect(() =>
      topologyFromApi({ ...REVIEW_DTO, default_trigger: "on_receive" }),
    ).toThrowError(/unknown default_trigger 'on_receive'/);
  });

  it("throws on unknown node_type", () => {
    expect(() =>
      topologyFromApi({
        ...REVIEW_DTO,
        nodes: [{ name: "x", node_type: "bogus", config: {}, trigger: null }],
      }),
    ).toThrowError(/unknown node_type 'bogus' for node 'x'/);
  });

  it("throws on non-string config.agent", () => {
    expect(() =>
      topologyFromApi({
        ...REVIEW_DTO,
        nodes: [
          { name: "x", node_type: "agent", config: { agent: 123 }, trigger: null },
        ],
      }),
    ).toThrowError(/non-string config.agent for node 'x'/);
  });
});
