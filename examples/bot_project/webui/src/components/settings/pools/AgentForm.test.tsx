import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type {
  ScopeAgentBill,
  ScopeOptions,
} from "../../../lib/scopeApi";
import { AgentForm } from "./AgentForm";
import type { AgentBody, AgentTreeNode } from "./scopeModel";

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
};

function node(body: AgentBody, path = ["main"]): AgentTreeNode {
  return { name: path[path.length - 1]!, path, body, children: [] };
}

function renderForm(body: AgentBody, path = ["main"]) {
  let updated: AgentBody | null = null;
  const view = render(
    <AgentForm
      pool="main"
      node={node(body, path)}
      options={OPTIONS}
      prompts={[]}
      issues={[]}
      bill={{ ...BILL, agent: path[path.length - 1]!, root: path.length === 1 }}
      updateAgent={(mut) => {
        updated = structuredClone(body);
        mut(updated);
      }}
      onApplyToPools={() => undefined}
    />,
  );
  return { view, updated: () => updated };
}

describe("AgentForm shell capability", () => {
  it("uses one shell row and reveals visibility only for main terminal mode", () => {
    const body: AgentBody = {
      capabilities: { shell: { future_option: { enabled: true } } },
    };
    const { view, updated } = renderForm(body);

    expect(screen.getAllByTestId("capability-row-shell")).toHaveLength(1);
    expect(screen.queryByLabelText("Enable terminal")).toBeNull();
    expect(screen.queryByLabelText("Visible terminal window")).toBeNull();
    expect(screen.getByRole("button", { name: "Shell mode" }).textContent).toContain(
      "Persistent",
    );

    fireEvent.click(screen.getByRole("button", { name: "Shell mode" }));
    fireEvent.click(screen.getByRole("option", { name: "Terminal" }));
    expect(updated()).toEqual({
      capabilities: {
        shell: { future_option: { enabled: true }, mode: "terminal" },
      },
    });

    view.rerender(
      <AgentForm
        pool="main"
        node={node(updated()!)}
        options={OPTIONS}
        prompts={[]}
        issues={[]}
        bill={BILL}
        updateAgent={() => undefined}
        onApplyToPools={() => undefined}
      />,
    );
    expect(screen.getByLabelText("Visible terminal window")).toBeTruthy();
  });

  it("shows a loaded subagent terminal request unchanged with a neutral downgrade hint", () => {
    const body: AgentBody = {
      capabilities: { shell: { mode: "terminal", future_option: 7 } },
    };
    const { updated } = renderForm(body, ["main", "child"]);

    expect(screen.getByRole("button", { name: "Shell mode" }).textContent).toContain(
      "Terminal",
    );
    expect(
      screen.getByText(
        "Terminal mode is main-agent only. This request is saved unchanged and runs as persistent for a subagent.",
      ),
    ).toBeTruthy();
    expect(screen.queryByLabelText("Visible terminal window")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Shell mode" }));
    fireEvent.click(screen.getByRole("option", { name: "Subprocess" }));
    expect(updated()).toEqual({
      capabilities: { shell: { mode: "subprocess", future_option: 7 } },
    });
  });

  it("does not render shell configuration for external agents", () => {
    renderForm({ execution_strategy: "external" });
    expect(screen.queryByTestId("capability-row-shell")).toBeNull();
    expect(screen.queryByRole("button", { name: "Shell mode" })).toBeNull();
  });
});
