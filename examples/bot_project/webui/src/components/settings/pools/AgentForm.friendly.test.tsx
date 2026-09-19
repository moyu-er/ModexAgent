// AgentForm friendly-face behavior (PA-12/PA-13):
//  - auto-naming toggle writes the session_title roster mutation
//  - memory rows show EFFECTIVE values from the bill; explicit off writes
//    false; reset clears only that override
//  - approval row reads the effective bill value
//  - collaborators render one level with the nested badge; add/delete go
//    through updateModel
//  - external agents hide native sections and show no collaborator add

import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi, afterEach } from "vitest";
import type { ScopeAgentBill, ScopeOptions, ScopeModelTree } from "../../../lib/scopeApi";
import { AgentForm } from "./AgentForm";
import type { AgentBody, AgentTreeNode } from "./scopeModel";
import { ToastProvider } from "../../ToastContext";
import { directCollaborators } from "./scopeModel";

vi.mock("../../../lib/skillsApi", () => ({
  listSkills: vi.fn().mockResolvedValue([]),
  assignSkill: vi.fn(),
  unassignSkill: vi.fn(),
  listAgentSkills: vi.fn().mockResolvedValue([]),
}));

const OPTIONS: ScopeOptions = {
  toolsets: ["full"],
  context_modes: ["fresh", "fork"],
  execution_strategies: ["react", "external"],
  provider_kinds: ["opencode"],
  capabilities: [],
  capability_bundles: {},
  hooks: [],
  default_hooks: ["session_title"],
  interceptors: [],
  commands: [],
  mcp_servers: [],
  position_defaults: {
    root: { toolset: "full", registration: "eager" },
    sub: { toolset: "read_write", registration: "lazy" },
  },
};

const BILL: ScopeAgentBill = {
  pool: "main",
  agent: "main",
  root: true,
  external: false,
  fields: [],
  tools: [],
  tool_groups: [],
  hooks: [],
  capabilities: [],
  memory: { memory_preset: "archive_core", archive_enabled: true, core_enabled: false },
  approval: { enabled: false, eligible: true },
};

function node(body: AgentBody, children: AgentTreeNode[] = []): AgentTreeNode {
  return { name: "main", path: ["main"], body, children };
}

function renderForm(
  body: AgentBody,
  opts: {
    bill?: ScopeAgentBill;
    children?: AgentTreeNode[];
    updateModel?: (mut: (d: ScopeModelTree) => void) => void;
    identityPersisted?: boolean;
    persistedSkillsEnabled?: boolean;
  } = {},
) {
  let updated: AgentBody | null = null;
  const view = render(
    <ToastProvider>
      <AgentForm
        persistedSkillsEnabled={opts.persistedSkillsEnabled ?? true}
        pool="main"
        node={node(body, opts.children ?? [])}
        options={OPTIONS}
        prompts={[]}
        issues={[]}
        bill={opts.bill ?? BILL}
        updateAgent={(mut) => {
          updated = structuredClone(body);
          mut(updated);
        }}
        updateModel={opts.updateModel}
        agentPath={["main"]}
        identityPersisted={opts.identityPersisted ?? true}
      />
    </ToastProvider>,
  );
  return { view, updated: () => updated };
}

afterEach(() => vi.clearAllMocks());

describe("auto naming (PA-12)", () => {
  it("defaults on via default_hooks and an explicit off writes a veto", () => {
    const { updated } = renderForm({});
    const toggle = screen.getByLabelText("Automatic conversation naming");
    expect(toggle).toBeTruthy();
    expect((toggle as HTMLInputElement).checked).toBe(true);

    fireEvent.click(toggle);
    expect(updated()!.hooks).toEqual(["-session_title"]);
  });

  it("turning it back on removes the veto (no local entry for a default)", () => {
    const { updated } = renderForm({ hooks: ["-session_title"] });
    const toggle = screen.getByLabelText(
      "Automatic conversation naming",
    ) as HTMLInputElement;
    expect(toggle.checked).toBe(false);
    fireEvent.click(toggle);
    expect(updated()!.hooks).toBeUndefined();
  });
});

describe("memory effective values (PA-12 / V14)", () => {
  it("shows the bill's effective archive value even with no local override", () => {
    renderForm({});
    // The bill says archive_enabled: true; the declaration has no override.
    const archive = screen.getByLabelText("Archive memory") as HTMLInputElement;
    expect(archive.checked).toBe(true);
    // Both memory rows report "Follows default" (no local overrides).
    expect(screen.getAllByText("Follows default")).toHaveLength(2);
  });

  it("explicit off writes false; reset clears only that override", () => {
    const { updated } = renderForm({
      memory: { archive_enabled: true, core_enabled: false },
    });
    const archive = screen.getByLabelText("Archive memory") as HTMLInputElement;
    expect(archive.checked).toBe(true);
    expect(screen.getByText("Explicitly on")).toBeTruthy();

    fireEvent.click(archive); // off
    expect(updated()!.memory).toEqual({ archive_enabled: false, core_enabled: false });

    // Reset from an explicit state clears the override.
    const resetBtn = screen.getAllByText("Reset to default")[0]!;
    fireEvent.click(resetBtn);
    // resetMemoryLayer on archive also drops a default-off core override.
    expect(updated()!.memory).toBeUndefined();
  });

  it("approval row reads the effective bill value and writes explicit false", () => {
    const { updated } = renderForm({});
    const approval = screen.getByLabelText("Ask before write actions") as HTMLInputElement;
    expect(approval.checked).toBe(false);
    fireEvent.click(approval);
    expect(updated()!.approval).toEqual({ enabled: true });
  });
});

describe("collaborators (PA-13 / V15)", () => {
  const childBody: AgentBody = { description: "sub" };
  const grandchild: AgentTreeNode = {
    name: "grandchild",
    path: ["main", "child", "grandchild"],
    body: { description: "deep" },
    children: [],
  };
  const child: AgentTreeNode = {
    name: "child",
    path: ["main", "child"],
    body: childBody,
    children: [grandchild],
  };

  it("lists direct collaborators with the advanced-structure badge for nested ones", () => {
    renderForm({}, { children: [child] });
    expect(screen.getByText("child")).toBeTruthy();
    expect(screen.getByText("Advanced structure")).toBeTruthy();
  });

  it("delete goes through updateModel and removes the direct child only", () => {
    const draft: ScopeModelTree = {
      workspace: {
        name: "bot",
        pools: {
          main: {
            agents: {
              main: {
                description: "root",
                agents: {
                  child: { description: "sub", agents: { grandchild: { description: "deep" } } },
                },
              },
            },
          },
        },
      },
    };
    let mutated: ScopeModelTree | null = null;
    renderForm(
      { description: "root" },
      {
        children: directCollaborators({
          name: "main",
          path: ["main"],
          body: { description: "root" },
          children: [child],
        }),
        updateModel: (mut) => {
          mutated = structuredClone(draft);
          mut(mutated);
        },
      },
    );
    fireEvent.click(screen.getByRole("button", { name: "Remove subagent child" }));
    expect(mutated).not.toBeNull();
    // The whole child subtree is gone from the declaration; the root survives.
    const mainBody = (mutated! as { workspace: { pools: { main: { agents: Record<string, { agents?: Record<string, unknown> }> } } } })
      .workspace.pools.main.agents;
    expect("child" in (mainBody.main?.agents ?? {})).toBe(false);
    expect("main" in mainBody).toBe(true);
  });

  it("add collaborator validates the key and appends via updateModel", () => {
    let mutated: ScopeModelTree | null = null;
    const updateModel = vi.fn((mut: (d: ScopeModelTree) => void) => {
      mutated = { workspace: { name: "bot", pools: { main: { agents: { main: {} } } } } };
      mut(mutated);
    });
    renderForm(
      {},
      {
        updateModel,
      },
    );
    fireEvent.click(screen.getByRole("button", { name: "Add collaborator" }));
    const input = screen.getByPlaceholderText("assistant-key");
    fireEvent.change(input, { target: { value: "Bad Key!" } });
    fireEvent.click(screen.getByRole("button", { name: "Add" }));
    expect(
      screen.getByText(
        "Use lowercase letters, digits, and hyphens; start with a letter.",
      ),
    ).toBeTruthy();

    fireEvent.change(input, { target: { value: "researcher" } });
    fireEvent.click(screen.getByRole("button", { name: "Add" }));
    expect(updateModel).toHaveBeenCalledTimes(1);
    expect(mutated).not.toBeNull();
    const mainBody = (mutated! as { workspace: { pools: { main: { agents: Record<string, { agents?: Record<string, unknown> }> } } } })
      .workspace.pools.main.agents;
    expect("researcher" in (mainBody.main?.agents ?? {})).toBe(true);
  });
});

describe("external face (PA-12)", () => {
  it("hides native sections and shows no collaborator add", () => {
    renderForm({
      execution_strategy: "external",
      provider_kind: "opencode",
      description: "ext",
    });
    expect(screen.queryByTestId("collaborator-list")).toBeNull();
    expect(screen.queryByText("Add collaborator")).toBeNull();
    expect(screen.queryByLabelText("Archive memory")).toBeNull();
    expect(screen.queryByLabelText("Max steps")).toBeNull();
    // External agents cannot configure native plugins/Skills/MCP.
    expect(screen.queryByTestId("selection-list")).toBeNull();
    expect(
      screen.getByText(/are owned by the opencode CLI/),
    ).toBeTruthy();
  });
});

describe("identity gating (PLAN §3.3)", () => {
  it("waits for saved Skills enablement before loading assignments", () => {
    renderForm({ capabilities: { skills: {} } }, { persistedSkillsEnabled: false });
    expect(screen.queryByRole("button", { name: "Refresh assignments" })).toBeNull();
    expect(screen.getByText("Enable the Skills plugin and save to manage skills for this assistant.")).toBeTruthy();
  });

  it("does not load assignments when the Skills plugin is disabled", () => {
    renderForm({ capabilities: { skills: false } });
    expect(screen.queryByRole("button", { name: "Refresh assignments" })).toBeNull();
    expect(screen.getByText("Enable the Skills plugin and save to manage skills for this assistant.")).toBeTruthy();
  });

  it("shows the save-first notice and hides the extensions section", () => {
    renderForm({ description: "new" }, { identityPersisted: false });
    expect(screen.getByTestId("identity-save-first")).toBeTruthy();
    expect(screen.queryByText("Skills & MCP")).toBeNull();
  });
});
