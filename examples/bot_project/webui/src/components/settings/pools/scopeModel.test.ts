// scopeModel.test.ts — unit tests for the assistants panel's
// declaration-tree helpers. The load-bearing invariants: hook +/- merge
// semantics behind the auto-naming toggle, capability tri-state writes that
// preserve unknown config keys, and the unified memory/approval mutations.

import { describe, expect, it } from "vitest";
import {
  addDeclaredHook,
  addPool,
  addSubagent,
  agentBodyOf,
  capabilityMode,
  deleteAgent,
  deletePool,
  directCollaborators,
  findAgent,
  hasNestedAgents,
  memoryOverride,
  nodeIdsByName,
  removeDeclaredHook,
  resetApproval,
  resetMemoryLayer,
  restoreHook,
  sessionTitleOn,
  setApprovalEnabled,
  setCapabilityConfigField,
  setCapabilityMode,
  setMemoryLayer,
  setSessionTitle,
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
    expect(pool?.agents[0]?.body).toEqual({ description: "" });
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

  it("changes one capability config field without dropping unknown keys", () => {
    const body: AgentBody = {
      capabilities: {
        shell: {
          mode: "persistent",
          terminal_visibility: true,
          future_option: { enabled: true },
        },
      },
    };
    setCapabilityConfigField(body, "shell", "mode", "subprocess");
    expect(body.capabilities).toEqual({
      shell: {
        mode: "subprocess",
        terminal_visibility: true,
        future_option: { enabled: true },
      },
    });
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

// ── Unified tri-state mutations (PA-10) ──────────────────────────────────────

describe("memory — setMemoryLayer / resetMemoryLayer", () => {
  it("on writes an explicit override, preserving the session block and sibling toggle", () => {
    const body: AgentBody = {
      memory: { session: { max_context_tokens: 4321 } },
    };
    setMemoryLayer(body, "archive_enabled", true);
    expect(body.memory).toEqual({
      archive_enabled: true,
      session: { max_context_tokens: 4321 },
    });
    expect(memoryOverride(body, "archive_enabled")).toBe(true);
    // absent key reads as "no local override"
    expect(memoryOverride(body, "core_enabled")).toBeNull();
  });

  it("off writes explicit false — never deletes (deleting would re-inherit)", () => {
    const body: AgentBody = { memory: { archive_enabled: true, core_enabled: true } };
    setMemoryLayer(body, "archive_enabled", false);
    // Turning archive off cascades core off (core requires archive).
    expect(body.memory).toEqual({ archive_enabled: false, core_enabled: false });
  });

  it("turning core on keeps archive as-is (the user manages the dependency)", () => {
    const body: AgentBody = { memory: { archive_enabled: true } };
    setMemoryLayer(body, "core_enabled", true);
    expect(body.memory).toEqual({ archive_enabled: true, core_enabled: true });
  });

  it("reset removes only the override key; session and unknown advanced keys survive", () => {
    const body: AgentBody = {
      memory: {
        archive_enabled: false,
        core_enabled: false,
        session: { max_context_tokens: 999 },
      },
    };
    resetMemoryLayer(body, "archive_enabled");
    expect(body.memory).toEqual({ session: { max_context_tokens: 999 } });
  });

  it("reset on archive always removes the dependent core override (HIGH2)", () => {
    // core=true without an archive override would be INVALID (core
    // requires archive; archive's reset restores the off default).
    const explicitCore: AgentBody = {
      memory: { archive_enabled: true, core_enabled: true },
    };
    resetMemoryLayer(explicitCore, "archive_enabled");
    expect(explicitCore.memory).toBeUndefined();

    // core=false is identical to the default it would inherit — removed.
    const offCore: AgentBody = {
      memory: { archive_enabled: true, core_enabled: false },
    };
    resetMemoryLayer(offCore, "archive_enabled");
    expect(offCore.memory).toBeUndefined();

    // session (an unrelated advanced key) still survives the archive reset.
    const withSession: AgentBody = {
      memory: {
        archive_enabled: true,
        core_enabled: true,
        session: { max_context_tokens: 999 },
      },
    };
    resetMemoryLayer(withSession, "archive_enabled");
    expect(withSession.memory).toEqual({ session: { max_context_tokens: 999 } });
  });

  it("reset on an absent override is a no-op", () => {
    const body: AgentBody = {};
    resetMemoryLayer(body, "core_enabled");
    expect(body.memory).toBeUndefined();
  });
});

describe("approval — setApprovalEnabled / resetApproval", () => {
  it("off writes explicit enabled:false and preserves the per-tool rules", () => {
    const body: AgentBody = {
      approval: { enabled: true, tools: { bash: { allowed_paths: ["./*"] } } },
    };
    setApprovalEnabled(body, false);
    expect(body.approval).toEqual({
      enabled: false,
      tools: { bash: { allowed_paths: ["./*"] } },
    });
  });

  it("on creates a minimal block when absent", () => {
    const body: AgentBody = {};
    setApprovalEnabled(body, true);
    expect(body.approval).toEqual({ enabled: true });
  });

  it("reset removes only `enabled` — per-tool rules survive (HIGH1)", () => {
    const body: AgentBody = {
      approval: { enabled: false, tools: { bash: { allowed_paths: ["./*"] } } },
    };
    resetApproval(body);
    expect(body.approval).toEqual({ tools: { bash: { allowed_paths: ["./*"] } } });
  });

  it("reset drops the container when only `enabled` was configured", () => {
    const body: AgentBody = { approval: { enabled: true } };
    resetApproval(body);
    expect(body.approval).toBeUndefined();
  });

  it("reset on an absent block or absent enabled key is a no-op", () => {
    const absent: AgentBody = {};
    resetApproval(absent);
    expect(absent.approval).toBeUndefined();
    const noEnabled: AgentBody = {
      approval: { tools: { bash: { allowed_paths: [] } } },
    };
    resetApproval(noEnabled);
    expect(noEnabled.approval).toEqual({ tools: { bash: { allowed_paths: [] } } });
  });

  it("reset keeps sibling agent fields untouched", () => {
    const body: AgentBody = {
      approval: { enabled: false },
      capabilities: { experience: {} },
      hooks: ["+reference_collector"],
      llm_provider_config: { custom_key: "custom_value" },
    };
    resetApproval(body);
    expect(body.approval).toBeUndefined();
    expect(body.capabilities).toEqual({ experience: {} });
    expect(body.hooks).toEqual(["+reference_collector"]);
    expect(body.llm_provider_config).toEqual({ custom_key: "custom_value" });
  });
});

describe("memory/approval mutations preserve the nested declaration tree", () => {
  it("mutating one agent's memory leaves nested children and other pools intact", () => {
    const model = makeModel();
    const root = agentBodyOf(model, "default", ["default"]);
    if (!root) throw new Error("root missing");
    setMemoryLayer(root, "archive_enabled", true);
    expect(findAgent(viewModel(model), "default", ["default", "child"])).not.toBeNull();
    expect(agentBodyOf(model, "review", ["reviewer"])).toEqual({
      description: "root",
    });
    // The pre-existing hooks/interceptors on the mutated root survive.
    expect(root.hooks).toEqual(["+reference_collector", "-length_guard"]);
    expect(root.interceptor_configs).toEqual({
      sandbox_guard: { sandbox: { backend: "host" } },
    });
  });
});

// ── session_title hook toggle + collaborators (PA-12/PA-13) ─────────────────

describe("sessionTitleOn / setSessionTitle", () => {
  it("defaults to the position default when nothing is declared", () => {
    const body: AgentBody = {};
    // default_hooks carry session_title in the shipped declaration roster.
    expect(sessionTitleOn(body, ["session_title"])).toBe(true);
    expect(sessionTitleOn(body, [])).toBe(false);
  });

  it("a declared +session_title turns it on; a veto turns it off", () => {
    const on: AgentBody = { hooks: ["+session_title"] };
    expect(sessionTitleOn(on, [])).toBe(true);
    const off: AgentBody = { hooks: ["-session_title"] };
    expect(sessionTitleOn(off, ["session_title"])).toBe(false);
  });

  it("setSessionTitle(true) on a default-on agent writes NO local entry (clean)", () => {
    const body: AgentBody = {};
    setSessionTitle(body, true, ["session_title"]);
    expect(body.hooks).toBeUndefined();
  });

  it("setSessionTitle(true) after a veto restores the default (removes the veto)", () => {
    const body: AgentBody = { hooks: ["+other_hook", "-session_title"] };
    setSessionTitle(body, true, ["session_title"]);
    expect(body.hooks).toEqual(["+other_hook"]);
  });

  it("setSessionTitle(false) vetoes even when default_hooks carry it (explicit off)", () => {
    const body: AgentBody = { hooks: ["+other_hook"] };
    setSessionTitle(body, false, ["session_title"]);
    expect(body.hooks).toEqual(["+other_hook", "-session_title"]);
  });

  it("setSessionTitle(false) without a default writes an explicit + entry only if needed", () => {
    // Not in default_hooks: turning ON writes +name; turning OFF removes it.
    const body: AgentBody = {};
    setSessionTitle(body, true, []);
    expect(body.hooks).toEqual(["+session_title"]);
    setSessionTitle(body, false, []);
    expect(body.hooks).toBeUndefined();
  });

  it("other hook entries always survive", () => {
    const body: AgentBody = { hooks: ["+reference_collector", "-length_guard"] };
    setSessionTitle(body, false, ["session_title"]);
    expect(body.hooks).toEqual([
      "+reference_collector",
      "-length_guard",
      "-session_title",
    ]);
  });
});

describe("collaborators — direct children view (PA-13)", () => {
  it("directCollaborators lists only the root's first level", () => {
    const view = viewModel(makeModel());
    const root = findAgent(view, "default", ["default"]);
    if (!root) throw new Error("root missing");
    expect(directCollaborators(root).map((c) => c.name)).toEqual(["child"]);
  });

  it("hasNestedAgents flags a collaborator with its own subagents", () => {
    const model = makeModel();
    addSubagent(model, "default", ["default", "child"], "grandchild");
    const view = viewModel(model);
    const root = findAgent(view, "default", ["default"]);
    if (!root) throw new Error("root missing");
    const child = directCollaborators(root)[0]!;
    expect(hasNestedAgents(child)).toBe(true);
    // A leaf collaborator has none.
    const leaf = findAgent(view, "default", ["default", "child", "grandchild"]);
    if (!leaf) throw new Error("leaf missing");
    expect(hasNestedAgents(leaf)).toBe(false);
  });
});
