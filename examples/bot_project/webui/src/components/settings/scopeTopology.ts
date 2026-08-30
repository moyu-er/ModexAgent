// scopeTopology.ts — ScopeTopology(REST 声明面)→ ParsedGraphTopology(共享
// TopologyCanvas 输入模型)的映射器(票据16;ADR-0043 同形不合并:画布组件
// 共享,数据模型分离,映射全部在此适配层)。
//
// 推导规则:
// - 节点:id 唯一性 = agent 以 `pool.agent` 限定(shipped 池复用 agent 名
//   — coder/review 都有 explore/general;且裸名会与 pool/workspace 节点撞
//   名,如 default 池的 root 也叫 default);层级经 nodeType 传达
//   (workspace/pool/agent → 图标 + sub 标签 + data-node-type)。
// - 边:workspace→pool 与 pool→root 为容纳边;agent→child 来自声明的
//   parent 引用(推导边);peer 边为池对池——声明是双向的(ADR-0019),每对
//   逻辑链只渲染一次(声明序先见者定向)。
// - pool-as-root(无 workspace 层)是同一条路径:没有 workspace 节点,
//   池节点直接成为顶层——零特判。

import type { ScopeTopology } from "../../lib/scopeApi";
import type {
  ParsedEdge,
  ParsedGraphTopology,
  ParsedNode,
} from "../graphs/yaml/parseGraphSpec";

/** 一个声明 agent 的画布节点 id(pool 限定,跨池唯一)。 */
export function scopeAgentNodeId(pool: string, agent: string): string {
  return `${pool}.${agent}`;
}

export function scopeTopologyToCanvas(topo: ScopeTopology): ParsedGraphTopology {
  const nodes: ParsedNode[] = [];
  const edges: ParsedEdge[] = [];
  const rootName = topo.workspace ?? topo.pools[0]?.name ?? "scope";

  if (topo.workspace !== null) {
    nodes.push({ name: topo.workspace, nodeType: "workspace", config: {} });
  }
  const seenPeerPairs = new Set<string>();
  for (const pool of topo.pools) {
    nodes.push({ name: pool.name, nodeType: "pool", config: {} });
    if (topo.workspace !== null) {
      edges.push({ source: topo.workspace, target: pool.name });
    }
    for (const agent of pool.agents) {
      const id = scopeAgentNodeId(pool.name, agent.name);
      nodes.push({
        name: id,
        nodeType: "agent",
        config: { pool: pool.name, agent: agent.name },
      });
      edges.push({
        source:
          agent.parent === null
            ? pool.name
            : scopeAgentNodeId(pool.name, agent.parent),
        target: id,
      });
    }
    for (const peer of pool.peers) {
      const pairKey = [pool.name, peer].sort().join("");
      if (seenPeerPairs.has(pairKey)) continue;
      seenPeerPairs.add(pairKey);
      edges.push({ source: pool.name, target: peer });
    }
  }

  return {
    name: rootName,
    scheduler: "parallel",
    defaultTrigger: "on_all_preds",
    nodes,
    edges,
    entryNode: rootName,
  };
}
