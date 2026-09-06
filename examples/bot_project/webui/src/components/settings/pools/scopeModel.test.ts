// scopeModel.test.ts — unit tests for the pools panel's declaration-tree
// helpers. The load-bearing invariants: bidirectional peer sync (V5 by
// construction), delete cleans up peer back-references, hook +/- merge
// semantics, and apply-to-pools copying the whole permissions block.

import { describe, expect, it } from "vitest";
import {
  addDeclaredHook,
  addPool,
  addSubagent,
  agentBodyOf,
  applyPermissionsToOtherPools,
  bundleCarriedHooks,
  capabilityMode,
  declaredInterceptors,
  deleteAgent,
  deletePool,
  findAgent,
  hookCandidates,
  interceptorOn,
  nodeIdsByName,
  removeDeclaredHook,
  restoreHook,
  setCapabilityMode,
  setInterceptor,
  setPeer,
  vetoedHooks,
  vetoHook,
  viewModel,
  type AgentBody,
} from "./scopeModel";
import type { ScopeModelTree } from "../../../lib/scopeApi";

function makeModel(): ScopeModelTree {
  return {
    workspace: {
      name: "bot",
      pools: {
        default: {
          peers: ["review"],
          agents: {
            default: {
              description: "root",
              hooks: ["+reference_collector", "-length_guard"],
              interceptors: ["+sandbox_guard"],
              interceptor_configs: {
                sandbox_guard: { sandbox: { backend: "host" } },
              },
              approval: { enabled: true },
              agents: {
                child: { description: "sub" },
              },
            },
          },
        },
        review: {
          peers: ["default"],
          agents: {
            reviewer: { description: "root" },
          },
        },
      },
    },
  };
}

describe("viewModel", () => {
  it("builds the workspace → pools → agents tree", () => {
    const view = viewModel(makeModel());
    expect(view.poolAsRoot).toBe(false);
    expect(view.workspaceName).toBe("bot");
    expect(view.pools.map((p) => p.name)).toEqual(["default", "review"]);
    const root = findAgent(view, "default", ["default"]);
    expect(root?.children.map((c) => c.name)).toEqual(["child"]);
  });

  it("handles the pool-as-root form", () => {
    const view = viewModel({ pool: { name: "solo", agents: { a: {} } } });
    expect(view.poolAsRoot).toBe(true);
    expect(view.pools.map((p) => p.name)).toEqual(["solo"]);
  });
});

describe("structure operations", () => {
  it("addPool creates a pool with a same-named root agent", () => {
    const model = makeModel();
    addPool(model, "new-pool");
    const view = viewModel(model);
    const pool = view.pools.find((p) => p.name === "new-pool");
    expect(pool?.agents.map((a) => a.name)).toEqual(["new-pool"]);
    expect(pool?.agents[0]?.body.use_terminal).toBe(false);
  });

  it("addSubagent nests under the parent path", () => {
    const model = makeModel();
    addSubagent(model, "default", ["default", "child"], "grandchild");
    const view = viewModel(model);
    expect(findAgent(view, "default", ["default", "child", "grandchild"])).not.toBeNull();
  });

  it("deletePool removes the pool and every peer back-reference", () => {
    const model = makeModel();
    deletePool(model, "review");
    const view = viewModel(model);
    expect(view.pools.map((p) => p.name)).toEqual(["default"]);
    const body = agentBodyOf(model, "default", ["default"]);
    expect(body).not.toBeNull();
    const defaultPool = (model.workspace as Record<string, Record<string, { peers?: string[] }>>)
      .pools?.["default"];
    expect(defaultPool?.peers ?? []).toEqual([]);
  });

  it("deleting a pool root agent deletes the whole pool", () => {
    const model = makeModel();
    deleteAgent(model, "review", ["reviewer"]);
    expect(viewModel(model).pools.map((p) => p.name)).toEqual(["default"]);
  });

  it("deleting a subagent leaves the pool and siblings intact", () => {
    const model = makeModel();
    deleteAgent(model, "default", ["default", "child"]);
    const view = viewModel(model);
    expect(findAgent(view, "default", ["default"])).not.toBeNull();
    expect(findAgent(view, "default", ["default", "child"])).toBeNull();
  });
});

describe("peers", () => {
  it("setPeer writes both sides of the edge", () => {
    const model = makeModel();
    addPool(model, "coder");
    setPeer(model, "default", "coder", true);
    const pools = (model.workspace as Record<string, Record<string, { peers?: string[] }>>).pools;
    expect(pools?.["default"]?.peers).toContain("coder");
    expect(pools?.["coder"]?.peers).toEqual(["default"]);
    setPeer(model, "coder", "default", false);
    expect(pools?.["default"]?.peers ?? []).not.toContain("coder");
    expect(pools?.["coder"]?.peers ?? []).toEqual([]);
  });
});

describe("hooks — veto/restore/add (C2)", () => {
  it("vetoing a position-default hook writes a -name entry", () => {
    const body: AgentBody = {};
    vetoHook(body, "deliver_retry");
    expect(body.hooks).toEqual(["-deliver_retry"]);
    expect(vetoedHooks(body)).toEqual(["deliver_retry"]);
  });

  it("vetoing a bundle-carried hook also writes -name (same merge-base veto)", () => {
    const body: AgentBody = {};
    vetoHook(body, "todo_trace"); // carried by the todo capability
    expect(body.hooks).toEqual(["-todo_trace"]);
  });

  it("restore removes the -name veto entry", () => {
    const body: AgentBody = { hooks: ["+model_choice_bind", "-length_guard"] };
    restoreHook(body, "length_guard");
    expect(body.hooks).toEqual(["+model_choice_bind"]);
    expect(vetoedHooks(body)).toEqual([]);
    restoreHook(body, "model_choice_bind"); // no-op: not a veto
    expect(body.hooks).toEqual(["+model_choice_bind"]);
  });

  it("a veto replaces a prior add of the same hook, and vice versa", () => {
    const body: AgentBody = { hooks: ["+user_notice_cleanup"] };
    vetoHook(body, "user_notice_cleanup");
    expect(body.hooks).toEqual(["-user_notice_cleanup"]);
    addDeclaredHook(body, "user_notice_cleanup");
    expect(body.hooks).toEqual(["+user_notice_cleanup"]);
  });

  it("addDeclaredHook writes +name; removeDeclaredHook drops only the add", () => {
    const body: AgentBody = {};
    addDeclaredHook(body, "reference_collector");
    expect(body.hooks).toEqual(["+reference_collector"]);
    removeDeclaredHook(body, "reference_collector");
    expect(body.hooks).toBeUndefined();
  });

  it("hookCandidates excludes bundle-carried and already-effective hooks", () => {
    const bundles = {
      todo: { tools: ["todo_write"], hooks: ["todo_trace", "todo_cleanup"] },
      experience: { tools: [], hooks: ["experience_review"] },
    };
    const carried = bundleCarriedHooks(bundles);
    const candidates = hookCandidates(
      ["reference_collector", "todo_trace", "experience_review", "model_choice_bind"],
      carried,
      new Set(["reference_collector"]),
    );
    // bundle-carried hooks are never offered as +name declarations.
    expect(candidates).toEqual(["model_choice_bind"]);
    expect(candidates).not.toContain("todo_trace");
    expect(candidates).not.toContain("experience_review");
    expect(candidates).not.toContain("reference_collector");
  });
});

describe("capabilities — tri-state (C1)", () => {
  it("force-on writes {}, force-off writes false, follow-auto removes the key", () => {
    const body: AgentBody = {};
    setCapabilityMode(body, "todo", "on");
    expect(body.capabilities).toEqual({ todo: {} });
    expect(capabilityMode(body, "todo")).toBe("on");
    setCapabilityMode(body, "todo", "off");
    expect(body.capabilities).toEqual({ todo: false });
    expect(capabilityMode(body, "todo")).toBe("off");
    setCapabilityMode(body, "todo", "auto");
    expect(body.capabilities).toBeUndefined();
    expect(capabilityMode(body, "todo")).toBe("auto");
  });

  it("drops the capabilities block when the last override is removed", () => {
    const body: AgentBody = { capabilities: { aci: {}, todo: false } };
    setCapabilityMode(body, "aci", "auto");
    expect(body.capabilities).toEqual({ todo: false });
    setCapabilityMode(body, "todo", "auto");
    expect(body.capabilities).toBeUndefined();
  });
});

describe("interceptors — effective roster", () => {
  it("declaredInterceptors strips the + prefix; add/remove keep the convention", () => {
    const body: AgentBody = {};
    setInterceptor(body, "sandbox_guard", true);
    expect(body.interceptors).toEqual(["+sandbox_guard"]);
    expect(declaredInterceptors(body)).toEqual(["sandbox_guard"]);
    expect(interceptorOn(body, "sandbox_guard")).toBe(true);
  });

  it("removing sandbox_guard drops its orphaned config block", () => {
    const body: AgentBody = {
      interceptors: ["+sandbox_guard"],
      interceptor_configs: { sandbox_guard: { sandbox: { backend: "host" } } },
    };
    setInterceptor(body, "sandbox_guard", false);
    expect(body.interceptors).toBeUndefined();
    expect(body.interceptor_configs).toBeUndefined();
  });
});

describe("applyPermissionsToOtherPools", () => {
  it("copies interceptors, configs, and approval onto sibling pool roots", () => {
    const model = makeModel();
    applyPermissionsToOtherPools(model, "default", ["default"]);
    const reviewer = agentBodyOf(model, "review", ["reviewer"]);
    expect(reviewer?.interceptors).toEqual(["+sandbox_guard"]);
    expect(reviewer?.interceptor_configs).toEqual({
      sandbox_guard: { sandbox: { backend: "host" } },
    });
    expect(reviewer?.approval).toEqual({ enabled: true });
    // Subagents are untouched — the block belongs to pool roots only.
    const child = agentBodyOf(model, "default", ["default", "child"]);
    expect(child?.interceptors).toBeUndefined();
  });

  it("skips external pool roots — the native permission face is meaningless there", () => {
    const model = makeModel();
    const pools = (model.workspace as Record<string, unknown>).pools as Record<
      string,
      unknown
    >;
    pools["opencode"] = {
      agents: {
        opencode: { description: "ext", execution_strategy: "external" },
      },
    };
    applyPermissionsToOtherPools(model, "default", ["default"]);
    const ext = agentBodyOf(model, "opencode", ["opencode"]);
    expect(ext?.interceptors).toBeUndefined();
    expect(ext?.approval).toBeUndefined();
  });
});

describe("nodeIdsByName", () => {
  it("maps a bare issue node name to tree node ids", () => {
    const view = viewModel(makeModel());
    expect(nodeIdsByName(view, "review")).toEqual(["pool/review"]);
    expect(nodeIdsByName(view, "reviewer")).toEqual(["agent/review/reviewer"]);
    expect(nodeIdsByName(view, "bot")).toEqual(["workspace"]);
  });
});
