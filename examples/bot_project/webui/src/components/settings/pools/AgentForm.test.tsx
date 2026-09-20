import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type {
  ScopeAgentBill,
  ScopeOptions,
} from "../../../lib/scopeApi";
import { AgentForm } from "./AgentForm";
import type { AgentBody, AgentTreeNode } from "./scopeModel";
import { ToastProvider } from "../../ToastContext";

vi.mock("../../../lib/skillsApi", () => ({
  listSkills: vi.fn().mockResolvedValue([]),
  assignSkill: vi.fn(),
  unassignSkill: vi.fn(),
  listAgentSkills: vi.fn().mockResolvedValue([]),
}));

const SHELL_GROUP = {
  anchor: "bash",
  origin: "capability_derived",
  capability: "shell",
  variants: [
    { name: "subprocess", tools: ["bash"] },
    { name: "persistent", tools: ["bash", "bash_input"] },
    { name: "terminal", tools: ["bash", "process", "terminal"] },
  ],
};

const OPTIONS: ScopeOptions = {
  toolsets: ["full", "read_write", "read_only", "none"],
  context_modes: ["fresh", "fork"],
  execution_strategies: ["react", "external"],
  provider_kinds: ["opencode"],
  capabilities: ["shell"],
  capability_bundles: {
    shell: {
      tools: [],
      tool_groups: [SHELL_GROUP],
      hooks: [],
      config_fields: {
        mode: {
          value_type: "string",
          default: "persistent",
          choices: ["subprocess", "persistent", "terminal"],
        },
        terminal_visibility: {
          value_type: "boolean",
          default: false,
          choices: [],
        },
      },
    },
  },
  hooks: [],
  default_hooks: [],
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
  tool_groups: [SHELL_GROUP],
  hooks: [],
  capabilities: [
    {
      capability: "shell",
      state: "auto",
      registration_source: "bundled",
      contributions: [],
    },
  ],
  memory: {
    memory_preset: "archive_core",
    archive_enabled: false,
    core_enabled: false,
  },
  approval: { enabled: false, eligible: true },
};

function node(body: AgentBody, path = ["main"]): AgentTreeNode {
  return { name: path[path.length - 1]!, path, body, children: [] };
}

function renderForm(body: AgentBody, path = ["main"], optionsOverride?: Partial<ScopeOptions>) {
  const options = optionsOverride ? { ...OPTIONS, ...optionsOverride } : OPTIONS;
  let updated: AgentBody | null = null;
  const view = render(
    <ToastProvider>
      <AgentForm
        persistedSkillsEnabled={true}
        pool="main"
        node={node(body, path)}
        options={options}
        prompts={[]}
        issues={[]}
        bill={{ ...BILL, agent: path[path.length - 1]!, root: path.length === 1 }}
        updateAgent={(mut) => {
          updated = structuredClone(body);
          mut(updated);
        }}
      />
    </ToastProvider>,
  );
  return { view, updated: () => updated };
}

describe("AgentForm capability enablement (corrections 3+4)", () => {
  it("renders capabilities as a bounded selection list with one checkbox per capability", () => {
    renderForm({});
    const list = screen.getByTestId("selection-list");
    expect(list).toBeTruthy();
    expect(screen.getByRole("checkbox", { name: "Command execution" })).toBeTruthy();
    // No tri-state dropdown, shell-mode picker, or terminal visibility.
    expect(screen.queryByRole("button", { name: "Shell mode" })).toBeNull();
    expect(screen.queryByLabelText("Visible terminal window")).toBeNull();
  });

  it("enabling writes 'on' while PRESERVING the existing custom config object", () => {
    // A draft custom config (e.g. written by an expert through the raw
    // Scope editor) must survive an ordinary enable toggle.
    const body: AgentBody = {
      capabilities: { shell: false },
    };
    const { updated } = renderForm(body);
    const box = screen.getByRole("checkbox", { name: "Command execution" });
    expect((box as HTMLInputElement).checked).toBe(false);
    fireEvent.click(box);
    // off -> on keeps the declared map (config object form), not a bare true.
    expect(updated()!.capabilities).toEqual({ shell: {} });
  });

  it("disabling writes the real false veto while preserving unrelated advanced config", () => {
    const body: AgentBody = {
      capabilities: {
        shell: { future_option: { enabled: true }, mode: "terminal" },
      },
      max_steps: 71,
    };
    const { updated } = renderForm(body);
    fireEvent.click(screen.getByRole("checkbox", { name: "Command execution" }));
    // on -> off writes the real false; sibling fields survive untouched.
    expect(updated()!.capabilities).toEqual({ shell: false });
    expect(updated()!.max_steps).toBe(71);
  });

  it("an unrelated ordinary edit preserves the expert's custom capability config", () => {
    // Editing the description through the friendly face must leave an
    // already-declared shell config object untouched — the draft only
    // ever mutates the field being edited.
    const body: AgentBody = {
      description: "before",
      capabilities: { shell: { future_option: { enabled: true }, mode: "terminal" } },
    };
    const { updated } = renderForm(body);
    fireEvent.change(screen.getByLabelText("Description"), {
      target: { value: "after" },
    });
    expect(updated()!.description).toBe("after");
    expect(updated()!.capabilities).toEqual({
      shell: { future_option: { enabled: true }, mode: "terminal" },
    });
  });

  it("bill-absent optional capability renders OFF; bill-auto capability renders ON", () => {
    // `experience` is an optional bundle absent from the bill (never
    // auto-applies here); `shell` reports state "auto". Only shell is
    // checked — an unconfigured optional plugin must not read as enabled.
    const overrides: Partial<ScopeOptions> = {
      capabilities: ["shell", "experience"],
      capability_bundles: {
        ...OPTIONS.capability_bundles,
        experience: { tools: ["experience"], tool_groups: [], hooks: [], config_fields: {} },
      },
    };
    const { updated } = renderForm({}, ["main"], overrides);
    const shell = screen.getByRole("checkbox", { name: "Command execution" }) as HTMLInputElement;
    const experience = screen.getByRole("checkbox", { name: "Experience learning" }) as HTMLInputElement;
    expect(shell.checked).toBe(true);
    expect(experience.checked).toBe(false);

    // Enabling the absent one writes the real declared-on deviation.
    fireEvent.click(experience);
    expect(updated()!.capabilities).toEqual({ experience: {} });
  });

  it("hides the technical face entirely — no runtime, hooks, permissions, or sandbox controls", () => {
    renderForm({});
    expect(screen.queryByText("Advanced fields")).toBeNull();
    expect(screen.queryByLabelText("Max steps")).toBeNull();
    expect(screen.queryByText("Effective tools")).toBeNull();
    expect(screen.queryByText("Effective hooks")).toBeNull();
    expect(screen.queryByText("Interceptors")).toBeNull();
    expect(screen.queryByText("Sandbox backend")).toBeNull();
    expect(screen.queryByText("Apply to other pools")).toBeNull();
  });

  it("does not render capabilities for external agents", () => {
    renderForm({ execution_strategy: "external" });
    expect(screen.queryByRole("checkbox", { name: "Command execution" })).toBeNull();
    expect(screen.queryByTestId("selection-list")).toBeNull();
  });
});
