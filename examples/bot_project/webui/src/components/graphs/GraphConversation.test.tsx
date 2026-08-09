import { describe, it, expect, vi, afterEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import { GraphConversation } from "./GraphConversation";
import {
  getSpec,
  getRuns,
  getInstance,
  runGraph,
  type GraphRunRecord,
  type GraphSpecResponse,
  type GraphInstance,
} from "../../lib/graphsApi";

vi.mock("../../lib/graphsApi", () => ({
  getSpec: vi.fn(),
  getRuns: vi.fn(),
  getInstance: vi.fn(),
  runGraph: vi.fn(),
}));

const mockGetSpec = vi.mocked(getSpec);
const mockGetRuns = vi.mocked(getRuns);
const mockGetInstance = vi.mocked(getInstance);
const mockRunGraph = vi.mocked(runGraph);

const SPEC_YAML = `
name: simple
version: "1.0"
scheduler: linear
default_trigger: on_all_preds
nodes:
  - name: worker
    node_type: agent
    config:
      agent: worker
      pool: default
edges:
  - source: __start__
    target: worker
  - source: worker
    target: __end__
`;

const SPEC_RESPONSE: GraphSpecResponse = {
  spec_id: "42",
  name: "simple",
  version: "1.0",
  yaml_content: SPEC_YAML,
};

function makeRun(overrides: Partial<GraphRunRecord> = {}): GraphRunRecord {
  return {
    record_id: "rec-1",
    graph_instance_id: "inst-1",
    user_input: { content: "hello graph" },
    output: [{ content: "result text" }],
    status: "completed",
    created_at: 1700000000000,
    updated_at: 1700000001000,
    ...overrides,
  };
}

const RUNNING_INSTANCE: GraphInstance = {
  spec_id: "42",
  graph_instance_id: "inst-1",
  status: "running",
  nodes: [
    { node_name: "worker", node_id: "node_1", status: "running" },
  ],
  result: null,
};

afterEach(() => {
  vi.restoreAllMocks();
});

describe("GraphConversation", () => {
  it("renders empty state when no runs exist", async () => {
    mockGetSpec.mockResolvedValue(SPEC_RESPONSE);
    mockGetRuns.mockResolvedValue([]);

    render(
      <GraphConversation
        workspaceId=""
        specId="42"
        onBack={vi.fn()}
        onOpenInstance={vi.fn()}
        onEditYaml={vi.fn()}
        onOpenInstances={vi.fn()}
      />,
    );

    await waitFor(() => {
      expect(
        screen.getByText("No runs yet. Send a message below to start."),
      ).toBeTruthy();
    });
  });

  it("renders run history with user input bubble and graph output", async () => {
    mockGetSpec.mockResolvedValue(SPEC_RESPONSE);
    mockGetRuns.mockResolvedValue([makeRun()]);

    render(
      <GraphConversation
        workspaceId=""
        specId="42"
        onBack={vi.fn()}
        onOpenInstance={vi.fn()}
        onEditYaml={vi.fn()}
        onOpenInstances={vi.fn()}
      />,
    );

    await waitFor(() => {
      expect(screen.getByText("hello graph")).toBeTruthy();
    });
    expect(screen.getByText("result text")).toBeTruthy();
  });

  it("disables composer when a run is active", async () => {
    mockGetSpec.mockResolvedValue(SPEC_RESPONSE);
    mockGetRuns.mockResolvedValue([makeRun({ status: "running" })]);
    mockGetInstance.mockResolvedValue(RUNNING_INSTANCE);

    render(
      <GraphConversation
        workspaceId=""
        specId="42"
        onBack={vi.fn()}
        onOpenInstance={vi.fn()}
        onEditYaml={vi.fn()}
        onOpenInstances={vi.fn()}
      />,
    );

    await waitFor(() => {
      expect(screen.getByPlaceholderText("Graph is running...")).toBeTruthy();
    });
    const textarea = screen.getByPlaceholderText("Graph is running...");
    expect(textarea.hasAttribute("disabled")).toBe(true);
  });

  it("disables send button when input is empty", async () => {
    mockGetSpec.mockResolvedValue(SPEC_RESPONSE);
    mockGetRuns.mockResolvedValue([]);

    render(
      <GraphConversation
        workspaceId=""
        specId="42"
        onBack={vi.fn()}
        onOpenInstance={vi.fn()}
        onEditYaml={vi.fn()}
        onOpenInstances={vi.fn()}
      />,
    );

    await waitFor(() => {
      expect(
        screen.getByText("No runs yet. Send a message below to start."),
      ).toBeTruthy();
    });

    const sendBtn = screen.getByRole("button", { name: "Send" });
    expect(sendBtn.hasAttribute("disabled")).toBe(true);
  });

  it("shows merged output for a completed run", async () => {
    mockGetSpec.mockResolvedValue(SPEC_RESPONSE);
    mockGetRuns.mockResolvedValue([
      makeRun({
        output: [
          { content: "line one" },
          { content: "line two" },
        ],
      }),
    ]);

    render(
      <GraphConversation
        workspaceId=""
        specId="42"
        onBack={vi.fn()}
        onOpenInstance={vi.fn()}
        onEditYaml={vi.fn()}
        onOpenInstances={vi.fn()}
      />,
    );

    await waitFor(() => {
      expect(screen.getByText("hello graph")).toBeTruthy();
    });
    expect(screen.getByText(/line one/)).toBeTruthy();
    expect(screen.getByText(/line two/)).toBeTruthy();
  });

  it("renders danger styling for a crashed run", async () => {
    mockGetSpec.mockResolvedValue(SPEC_RESPONSE);
    mockGetRuns.mockResolvedValue([makeRun({ status: "crashed", output: null })]);

    render(
      <GraphConversation
        workspaceId=""
        specId="42"
        onBack={vi.fn()}
        onOpenInstance={vi.fn()}
        onEditYaml={vi.fn()}
        onOpenInstances={vi.fn()}
      />,
    );

    await waitFor(() => {
      expect(screen.getByText("crashed")).toBeTruthy();
    });
    const badge = screen.getByText("crashed");
    expect(badge.className).toContain("text-danger");
    expect(badge.className).toContain("border-danger");
  });

  it("calls runGraph when sending a message", async () => {
    mockGetSpec.mockResolvedValue(SPEC_RESPONSE);
    mockGetRuns.mockResolvedValue([]);
    mockRunGraph.mockResolvedValue({
      graph_instance_id: "inst-new",
      status: "pending",
    });

    render(
      <GraphConversation
        workspaceId=""
        specId="42"
        onBack={vi.fn()}
        onOpenInstance={vi.fn()}
        onEditYaml={vi.fn()}
        onOpenInstances={vi.fn()}
      />,
    );

    await waitFor(() => {
      expect(
        screen.getByText("No runs yet. Send a message below to start."),
      ).toBeTruthy();
    });

    const textarea = screen.getByPlaceholderText(
      "Send a message to run the graph...",
    );
    fireEvent.change(textarea, { target: { value: "run this" } });

    const sendBtn = screen.getByRole("button", { name: "Send" });
    expect(sendBtn.hasAttribute("disabled")).toBe(false);
    fireEvent.click(sendBtn);

    await waitFor(() => {
      expect(mockRunGraph).toHaveBeenCalledWith("", "42", "run this");
    });
  });

  it("calls onOpenInstance when View execution is clicked", async () => {
    const onOpenInstance = vi.fn();
    mockGetSpec.mockResolvedValue(SPEC_RESPONSE);
    mockGetRuns.mockResolvedValue([makeRun()]);

    render(
      <GraphConversation
        workspaceId=""
        specId="42"
        onBack={vi.fn()}
        onOpenInstance={onOpenInstance}
        onEditYaml={vi.fn()}
        onOpenInstances={vi.fn()}
      />,
    );

    await waitFor(() => {
      expect(screen.getByText("View execution details")).toBeTruthy();
    });
    fireEvent.click(screen.getByText("View execution details"));
    expect(onOpenInstance).toHaveBeenCalledWith("inst-1");
  });
});
