// topologyFromApi — GET /api/graphs/specs/{id}/topology DTO(§11.3)→ 渲染模型。
//
// 只读渲染(spec 列表缩略图 / spec 详情画布 / instance 拓扑)的唯一拓扑来源:
// 结构由后端 GraphSpec 序列化,后端新增字段(如 state_schema)不再影响渲染。
// 编辑器草稿校验仍走 parseGraphSpecYaml(解析用户正在编辑的 YAML 文本,不经
// 后端);两条入口共用 withBoundaryNodes 合成虚拟端点。

import type { GraphTopology } from "../../lib/graphsApi";
import {
  FUNCTIONAL_NODE_TYPES,
  GRAPH_NODE_START,
  withBoundaryNodes,
  type ParsedEdge,
  type ParsedGraphTopology,
  type ParsedNode,
  type ParsedNodeConfig,
} from "./yaml/parseGraphSpec";

const SCHEDULERS = ["linear", "parallel"] as const;

function extractNodeConfig(
  name: string,
  raw: Record<string, unknown> | undefined,
): ParsedNodeConfig {
  const config: ParsedNodeConfig = {};
  if (!raw) return config;
  for (const key of ["agent", "pool"] as const) {
    const value = raw[key];
    if (value === undefined || value === null) continue;
    if (typeof value !== "string") {
      throw new Error(
        `topology endpoint returned non-string config.${key} for node '${name}'`,
      );
    }
    config[key] = value;
  }
  return config;
}

/**
 * Map the topology-endpoint DTO to the rendering model. Throws on shapes the
 * rendering model cannot represent (backend/frontend drift) so callers can
 * surface the error instead of silently rendering an empty canvas.
 */
export function topologyFromApi(api: GraphTopology): ParsedGraphTopology {
  const scheduler = SCHEDULERS.find((s) => s === api.scheduler);
  if (scheduler === undefined) {
    throw new Error(
      `topology endpoint returned unknown scheduler '${api.scheduler}'`,
    );
  }
  if (api.default_trigger !== "on_all_preds") {
    throw new Error(
      `topology endpoint returned unknown default_trigger '${api.default_trigger}'`,
    );
  }

  const declaredNodes: ParsedNode[] = api.nodes.map((n) => {
    const nodeType = FUNCTIONAL_NODE_TYPES.find((t) => t === n.node_type);
    if (nodeType === undefined) {
      throw new Error(
        `topology endpoint returned unknown node_type '${n.node_type}' for node '${n.name}'`,
      );
    }
    const node: ParsedNode = {
      name: n.name,
      nodeType,
      config: extractNodeConfig(n.name, n.config),
    };
    if (typeof n.trigger === "string") node.trigger = n.trigger;
    return node;
  });

  const edges: ParsedEdge[] = api.edges.map((e) => ({
    source: e.source,
    target: e.target,
  }));

  return {
    name: api.name,
    scheduler,
    defaultTrigger: "on_all_preds",
    nodes: withBoundaryNodes(declaredNodes, edges),
    edges,
    entryNode: GRAPH_NODE_START,
  };
}
