// scopeTopology mapper tests (ticket 16) — structural assertions over the
// three declaration shapes: workspace-hosted multi-pool (with bidirectional
// peer links), pool-as-root (no workspace layer), and a deep-nested tree.

import { describe, it, expect } from "vitest";
import type { ScopeTopology } from "../../lib/scopeApi";
import { scopeAgentNodeId, scopeTopologyToCanvas } from "./scopeTopology";

/** Shipped-like workspace form: 4 pools, peer links declared on both sides,
 *  cross-pool agent name reuse (explore/general in coder AND review). */
const WORKSPACE_SHAPE: ScopeTopology = {
  kind: "workspace",
  workspace: "bot",
  pools: [
    {
      name: "default",
      peers: ["opencode", "review"],
      agents: [
        { name: "default", parent: null, root: true },
        { name: "office-expert", parent: "default", root: false },
      ],
    },
    {
      name: "coder",
      peers: [],
      agents: [
        { name: "orchestrator", parent: null, root: true },
        { name: "explore", parent: "orchestrator", root: false },
        { name: "general", parent: "orchestrator", root: false },
      ],
    },
    {
      name: "review",
      peers: ["default"],
      agents: [
        { name: "reviewer", parent: null, root: true },
        { name: "explore", parent: "reviewer", root: false },
        { name: "general", parent: "reviewer", root: false },
      ],
    },
    {
      name: "opencode",
      peers: ["default"],
      agents: [{ name: "opencode", parent: null, root: true }],
    },
  ],
};

const POOL_ROOT_SHAPE: ScopeTopology = {
  kind: "pool",
  workspace: null,
  pools: [
    {
      name: "solo",
      peers: [],
      agents: [{ name: "solo", parent: null, root: true }],
    },
  ],
};

const NESTED_SHAPE: ScopeTopology = {
  kind: "pool",
  workspace: null,
  pools: [
    {
      name: "deep",
      peers: [],
      agents: [
        { name: "a", parent: null, root: true },
        { name: "b", parent: "a", root: false },
        { name: "c", parent: "b", root: false },
      ],
    },
  ],
};

function edgeSet(topo: ReturnType<typeof scopeTopologyToCanvas>): Set<string> {
  return new Set(topo.edges.map((e) => `${e.source}->${e.target}`));
}

describe("scopeTopologyToCanvas — workspace shape", () => {
  const canvas = scopeTopologyToCanvas(WORKSPACE_SHAPE);

  it("node count + level labels (1 workspace / 4 pools / 9 agents)", () => {
    expect(canvas.nodes).toHaveLength(14);
    const byType = new Map<string, number>();
    for (const node of canvas.nodes) {
      byType.set(node.nodeType, (byType.get(node.nodeType) ?? 0) + 1);
    }
    expect(byType.get("workspace")).toBe(1);
    expect(byType.get("pool")).toBe(4);
    expect(byType.get("agent")).toBe(9);
  });

  it("agent node ids are pool-qualified (cross-pool name reuse stays unique)", () => {
    const ids = canvas.nodes.map((n) => n.name);
    expect(new Set(ids).size).toBe(ids.length);
    expect(ids).toContain("coder.explore");
    expect(ids).toContain("review.explore");
    // The bare agent name would collide with the pool node itself.
    expect(ids).toContain("default.default");
    const explore = canvas.nodes.find((n) => n.name === "coder.explore");
    expect(explore?.config).toEqual({ pool: "coder", agent: "explore" });
  });

  it("containment + parent-derived edges", () => {
    const edges = edgeSet(canvas);
    for (const pool of ["default", "coder", "review", "opencode"]) {
      expect(edges.has(`bot->${pool}`)).toBe(true);
    }
    expect(edges.has("default->default.default")).toBe(true);
    expect(edges.has("default.default->default.office-expert")).toBe(true);
    expect(edges.has("coder->coder.orchestrator")).toBe(true);
    expect(edges.has("coder.orchestrator->coder.explore")).toBe(true);
    expect(edges.has("review->review.reviewer")).toBe(true);
  });

  it("peer links render once per logical pair (bidirectional declaration)", () => {
    const edges = edgeSet(canvas);
    expect(edges.has("default->opencode")).toBe(true);
    expect(edges.has("default->review")).toBe(true);
    // Declared on both sides — the reverse direction is NOT re-rendered.
    expect(edges.has("opencode->default")).toBe(false);
    expect(edges.has("review->default")).toBe(false);
    // 4 ws→pool + 4 pool→root + 5 parent + 2 peer.
    expect(canvas.edges).toHaveLength(15);
  });
});

describe("scopeTopologyToCanvas — pool-as-root (no workspace layer)", () => {
  it("renders through the same path: pool node at top, zero special-casing", () => {
    const canvas = scopeTopologyToCanvas(POOL_ROOT_SHAPE);
    expect(canvas.nodes).toHaveLength(2);
    expect(canvas.nodes[0]).toMatchObject({ name: "solo", nodeType: "pool" });
    expect(canvas.nodes[1]).toMatchObject({
      name: "solo.solo",
      nodeType: "agent",
    });
    expect(edgeSet(canvas).has("solo->solo.solo")).toBe(true);
    expect(canvas.edges).toHaveLength(1);
    expect(canvas.entryNode).toBe("solo");
  });
});

describe("scopeTopologyToCanvas — deep-nested tree", () => {
  it("three agent levels chain through parent references", () => {
    const canvas = scopeTopologyToCanvas(NESTED_SHAPE);
    expect(canvas.nodes).toHaveLength(4);
    const edges = edgeSet(canvas);
    expect(edges.has("deep->deep.a")).toBe(true);
    expect(edges.has("deep.a->deep.b")).toBe(true);
    expect(edges.has("deep.b->deep.c")).toBe(true);
    expect(canvas.edges).toHaveLength(3);
  });
});

describe("scopeAgentNodeId", () => {
  it("qualifies with the pool name", () => {
    expect(scopeAgentNodeId("coder", "explore")).toBe("coder.explore");
  });
});
