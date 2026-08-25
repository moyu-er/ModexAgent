import { describe, it, expect, vi, afterEach, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent, within } from "@testing-library/react";
import { GraphInstanceDetail } from "./GraphInstanceDetail";
import { useGraphExecution } from "../../hooks/useGraphExecution";
import {
  getSpec,
  getTopology,
  getInvocations,
  invokeInstance,
  deliverToNode,
  pauseGraph,
  resumeGraph,
  stopGraph,
  type GraphInstance,
  type GraphInvocationRecord,
  type GraphSpecResponse,
  type GraphTopology,
} from "../../lib/graphsApi";
import { ToastProvider } from "../ToastContext";

vi.mock("../../hooks/useGraphExecution", () => ({
  useGraphExecution: vi.fn(),
}));

vi.mock("../../lib/graphsApi", () => ({
  getSpec: vi.fn(),
  getTopology: vi.fn(),
  getInvocations: vi.fn(),
  invokeInstance: vi.fn(),
  deliverToNode: vi.fn(),
  pauseGraph: vi.fn(),
  resumeGraph: vi.fn(),
  stopGraph: vi.fn(),
}));

const mockUseGraphExecution = vi.mocked(useGraphExecution);
const mockGetSpec = vi.mocked(getSpec);
const mockGetTopology = vi.mocked(getTopology);
const mockGetInvocations = vi.mocked(getInvocations);
const mockInvokeInstance = vi.mocked(invokeInstance);
const mockDeliverToNode = vi.mocked(deliverToNode);
const mockPauseGraph = vi.mocked(pauseGraph);
const mockResumeGraph = vi.mocked(resumeGraph);
const mockStopGraph = vi.mocked(stopGraph);

const SPEC_YAML = `
name: review_workflow
version: "1.0"
scheduler: parallel
default_trigger: on_all_preds
nodes:
  - name: designer
    node_type: agent
    config:
      agent: designer
      pool: review
  - name: reviewer
    node_type: agent
    config:
      agent: review
edges:
  - source: __start__
    target: designer
  - source: designer
    target: reviewer
  - source: reviewer
    target: __end__
`;

const SPEC_RESPONSE: GraphSpecResponse = {
  spec_id: "spec_1",
  name: "review_workflow",
  version: "1.0",
  yaml_content: SPEC_YAML,
};

const TOPOLOGY_DTO: GraphTopology = {
  spec_id: "spec_1",
  name: "review_workflow",
  scheduler: "parallel",
  default_trigger: "on_all_preds",
  nodes: [
    {
      name: "designer",
      node_type: "agent",
      config: { agent: "designer", pool: "review" },
      trigger: null,
    },
    {
      name: "reviewer",
      node_type: "agent",
      config: { agent: "review" },
      trigger: null,
    },
  ],
  edges: [
    { source: "__start__", target: "designer" },
    { source: "designer", target: "reviewer" },
    { source: "reviewer", target: "__end__" },
  ],
  entry_node: "__start__",
};

function makeInstance(overrides: Partial<GraphInstance> = {}): GraphInstance {
  return {
    spec_id: "spec_1",
    graph_instance_id: "12345",
    status: "completed",
    nodes: [
      { node_name: "designer", node_id: "n1", status: "completed", session_id: "sess.designer" },
      { node_name: "reviewer", node_id: "n2", status: "completed" },
    ],
    result: null,
    ...overrides,
  };
}

function makeInvocation(
  overrides: Partial<GraphInvocationRecord> = {},
): GraphInvocationRecord {
  return {
    record_id: "rec_1",
    version: 1,
    user_input: { content: "Review PR #42" },
    output: [{ content: "PR looks good." }],
    created_at: 1000000,
    ...overrides,
  };
}

function mockHook(overrides: {
  instance?: GraphInstance | null;
  pulses?: unknown[];
  error?: string | null;
} = {}): void {
  mockUseGraphExecution.mockReturnValue({
    instance: overrides.instance ?? makeInstance(),
    timeline: [],
    pulses: (overrides.pulses ?? []) as never,
    crashFlashes: [],
    error: overrides.error ?? null,
    refresh: vi.fn(),
    dismissPulse: vi.fn(),
    dismissCrashFlash: vi.fn(),
  } as never);
}

function renderDetail(props?: {
  workspaceId?: string;
  instanceId?: string;
  onBack?: ReturnType<typeof vi.fn>;
  onJumpToSession?: ReturnType<typeof vi.fn>;
}): void {
  render(
    <ToastProvider>
      <GraphInstanceDetail
        workspaceId={props?.workspaceId ?? ""}
        instanceId={props?.instanceId ?? "12345"}
        onBack={props?.onBack ?? vi.fn()}
        onJumpToSession={props?.onJumpToSession ?? vi.fn()}
      />
    </ToastProvider>,
  );
}

async function waitForDetail(): Promise<void> {
  await waitFor(() => {
    expect(screen.getByTestId("graph-instance-detail")).toBeTruthy();
  });
}

beforeEach(() => {
  mockUseGraphExecution.mockReset();
  mockGetSpec.mockReset();
  mockGetTopology.mockReset();
  mockGetInvocations.mockReset();
  mockInvokeInstance.mockReset();
  mockDeliverToNode.mockReset();
  mockPauseGraph.mockReset();
  mockResumeGraph.mockReset();
  mockStopGraph.mockReset();

  mockHook();
  mockGetSpec.mockResolvedValue(SPEC_RESPONSE);
  mockGetTopology.mockResolvedValue(TOPOLOGY_DTO);
  mockGetInvocations.mockResolvedValue([makeInvocation()]);
  mockInvokeInstance.mockResolvedValue({
    graph_instance_id: "12345",
    status: "running",
  });
  mockDeliverToNode.mockResolvedValue({
    graph_instance_id: "12345",
    node_name: "designer",
    status: "ok",
  });
  mockPauseGraph.mockResolvedValue({
    graph_instance_id: "12345",
    status: "paused",
  });
  mockResumeGraph.mockResolvedValue({
    graph_instance_id: "12345",
    status: "running",
  });
  mockStopGraph.mockResolvedValue({
    graph_instance_id: "12345",
    status: "stopped",
  });
});

afterEach(() => {
  vi.clearAllMocks();
});

describe("GraphInstanceDetail — header", () => {
  it("renders header with back button, instance ID, spec name, spec version badge, and status badge", async () => {
    renderDetail();
    await waitForDetail();

    expect(screen.getByText("Spec")).toBeTruthy();
    expect(screen.getByText("#12345")).toBeTruthy();
    expect(screen.getByText("review_workflow")).toBeTruthy();
    expect(screen.getByText("spec v1.0")).toBeTruthy();
    expect(screen.getByText("completed")).toBeTruthy();
  });

  it("renders Topology toggle button", async () => {
    renderDetail();
    await waitForDetail();

    const openButton = screen.getByText("Topology");
    expect(openButton.getAttribute("aria-haspopup")).toBe("dialog");
  });

  it("calls onBack when back button is clicked", async () => {
    const onBack = vi.fn();
    renderDetail({ onBack });
    await waitForDetail();

    fireEvent.click(screen.getByText("Spec"));
    expect(onBack).toHaveBeenCalledOnce();
  });
});

describe("GraphInstanceDetail — conversation flow", () => {
  it("renders user input bubble and graph output bubble", async () => {
    renderDetail();
    await waitForDetail();

    const userBubble = screen.getByText("Review PR #42");
    expect(userBubble).toBeTruthy();
    expect(userBubble.closest(".bubble-user")).toBeTruthy();

    const outputBubble = screen.getByText("PR looks good.");
    expect(outputBubble).toBeTruthy();
  });

  it("renders no-invocations message when list is empty", async () => {
    mockGetInvocations.mockResolvedValue([]);
    renderDetail();
    await waitForDetail();

    expect(screen.getByText("No invocations yet. Send a message below to start.")).toBeTruthy();
  });

  it("renders multiple invocations as conversation entries", async () => {
    mockGetInvocations.mockResolvedValue([
      makeInvocation({
        record_id: "rec_1",
        version: 1,
        user_input: { content: "First input" },
        output: [{ content: "First output" }],
        created_at: 1000000,
      }),
      makeInvocation({
        record_id: "rec_2",
        version: 2,
        user_input: { content: "Second input" },
        output: [{ content: "Second output" }],
        created_at: 2000000,
      }),
    ]);
    renderDetail();
    await waitForDetail();

    expect(screen.getByText("First input")).toBeTruthy();
    expect(screen.getByText("First output")).toBeTruthy();
    expect(screen.getByText("Second input")).toBeTruthy();
    expect(screen.getByText("Second output")).toBeTruthy();
  });

  it("renders typing dots when latest invocation is running", async () => {
    mockHook({ instance: makeInstance({ status: "running" }) });
    mockGetInvocations.mockResolvedValue([
      makeInvocation({ output: null }),
    ]);
    renderDetail();
    await waitForDetail();

    const dots = screen.getByTestId("graph-instance-detail").querySelector(".typing-dots");
    expect(dots).toBeTruthy();
  });
});

describe("GraphInstanceDetail — composer", () => {
  it("enables composer when instance is terminal (completed)", async () => {
    renderDetail();
    await waitForDetail();

    const textarea = screen.getByPlaceholderText("Re-invoke this instance...");
    expect(textarea).toBeTruthy();
    expect((textarea as HTMLTextAreaElement).disabled).toBe(false);
  });

  it("disables composer when instance is running", async () => {
    mockHook({ instance: makeInstance({ status: "running" }) });
    renderDetail();
    await waitForDetail();

    const textarea = screen.getByPlaceholderText("Graph is running... wait for completion");
    expect(textarea).toBeTruthy();
    expect((textarea as HTMLTextAreaElement).disabled).toBe(true);
  });

  it("calls invokeInstance when Invoke button is clicked with input", async () => {
    renderDetail();
    await waitForDetail();

    const textarea = screen.getByPlaceholderText("Re-invoke this instance...");
    fireEvent.change(textarea, { target: { value: "Re-run with new input" } });

    const invokeButton = screen.getByRole("button", { name: "Invoke" });
    fireEvent.click(invokeButton);

    await waitFor(() => {
      expect(mockInvokeInstance).toHaveBeenCalledWith("", "12345", "Re-run with new input");
    });
  });

  it("disables Invoke button when input is empty", async () => {
    renderDetail();
    await waitForDetail();

    const invokeButton = screen.getByRole("button", { name: "Invoke" }) as HTMLButtonElement;
    expect(invokeButton.disabled).toBe(true);
  });

  it("disables Invoke button when running", async () => {
    mockHook({ instance: makeInstance({ status: "running" }) });
    renderDetail();
    await waitForDetail();

    const invokeButton = screen.getByRole("button", { name: "Invoke" }) as HTMLButtonElement;
    expect(invokeButton.disabled).toBe(true);
  });

  it("clears input after successful invoke", async () => {
    renderDetail();
    await waitForDetail();

    const textarea = screen.getByPlaceholderText("Re-invoke this instance...") as HTMLTextAreaElement;
    fireEvent.change(textarea, { target: { value: "New input" } });

    fireEvent.click(screen.getByRole("button", { name: "Invoke" }));

    await waitFor(() => {
      expect(textarea.value).toBe("");
    });
  });

  it("submits on Enter key (without Shift)", async () => {
    renderDetail();
    await waitForDetail();

    const textarea = screen.getByPlaceholderText("Re-invoke this instance...");
    fireEvent.change(textarea, { target: { value: "Enter submit" } });
    fireEvent.keyDown(textarea, { key: "Enter", shiftKey: false });

    await waitFor(() => {
      expect(mockInvokeInstance).toHaveBeenCalledWith("", "12345", "Enter submit");
    });
  });
});

describe("GraphInstanceDetail — run graph modal", () => {
  it("does not render the run graph dialog by default", async () => {
    renderDetail();
    await waitForDetail();

    expect(screen.queryByTestId("run-graph-modal")).toBeNull();
  });

  it("opens the run graph dialog when Topology button is clicked", async () => {
    renderDetail();
    await waitForDetail();

    fireEvent.click(screen.getByText("Topology"));

    await waitFor(() => {
      expect(screen.getByTestId("run-graph-modal")).toBeTruthy();
    });
    expect(screen.getByTestId("run-graph-backdrop")).toBeTruthy();
  });

  it("dialog has role=dialog and aria-modal=true", async () => {
    renderDetail();
    await waitForDetail();

    fireEvent.click(screen.getByText("Topology"));

    await waitFor(() => {
      const dialog = screen.getByRole("dialog");
      expect(dialog.getAttribute("aria-modal")).toBe("true");
    });
  });

  it("dialog shows control bar with Pause and Stop when running", async () => {
    mockHook({ instance: makeInstance({ status: "running" }) });
    renderDetail();
    await waitForDetail();

    fireEvent.click(screen.getByText("Topology"));

    await waitFor(() => {
      expect(screen.getByTestId("run-graph-modal")).toBeTruthy();
    });
    const bar = within(screen.getByTestId("run-graph-modal")).getByTestId("control-bar");
    expect(within(bar).getByText("Pause")).toBeTruthy();
    expect(within(bar).getByText("Stop")).toBeTruthy();
  });

  it("closes dialog when X button is clicked", async () => {
    renderDetail();
    await waitForDetail();

    fireEvent.click(screen.getByText("Topology"));
    await waitFor(() => {
      expect(screen.getByTestId("run-graph-modal")).toBeTruthy();
    });

    const dialog = screen.getByTestId("run-graph-modal");
    fireEvent.click(within(dialog).getByLabelText("Close"));

    await waitFor(() => {
      expect(screen.queryByTestId("run-graph-modal")).toBeNull();
    });
  });

  it("closes dialog on Escape key", async () => {
    renderDetail();
    await waitForDetail();

    fireEvent.click(screen.getByText("Topology"));
    await waitFor(() => {
      expect(screen.getByTestId("run-graph-modal")).toBeTruthy();
    });

    fireEvent.keyDown(window, { key: "Escape" });

    await waitFor(() => {
      expect(screen.queryByTestId("run-graph-modal")).toBeNull();
    });
  });

  it("closes dialog on backdrop click", async () => {
    renderDetail();
    await waitForDetail();

    fireEvent.click(screen.getByText("Topology"));
    await waitFor(() => {
      expect(screen.getByTestId("run-graph-backdrop")).toBeTruthy();
    });

    fireEvent.click(screen.getByTestId("run-graph-backdrop"));

    await waitFor(() => {
      expect(screen.queryByTestId("run-graph-modal")).toBeNull();
    });
  });

  it("renders TopologyCanvas inside the dialog", async () => {
    renderDetail();
    await waitForDetail();

    fireEvent.click(screen.getByText("Topology"));

    await waitFor(() => {
      expect(screen.getByTestId("topology-canvas")).toBeTruthy();
    });
  });

  it("renders instance summary meta (scheduler, trigger) inside the dialog", async () => {
    renderDetail();
    await waitForDetail();

    fireEvent.click(screen.getByText("Topology"));

    await waitFor(() => {
      const dialog = screen.getByTestId("run-graph-modal");
      expect(within(dialog).getByText("parallel")).toBeTruthy();
      expect(within(dialog).getByText("on_all_preds")).toBeTruthy();
    });
  });

  it("moves focus into the dialog on open and returns focus to the Topology button on close", async () => {
    renderDetail();
    await waitForDetail();

    const openButton = screen.getByText("Topology");
    (openButton as HTMLElement).focus();
    fireEvent.click(openButton);

    await waitFor(() => {
      const dialog = screen.getByTestId("run-graph-modal");
      expect(dialog.contains(document.activeElement)).toBe(true);
    });

    fireEvent.keyDown(window, { key: "Escape" });

    await waitFor(() => {
      expect(screen.queryByTestId("run-graph-modal")).toBeNull();
    });
    expect(document.activeElement).toBe(openButton);
  });

  it("keeps focus inside the dialog on Tab", async () => {
    renderDetail();
    await waitForDetail();

    fireEvent.click(screen.getByText("Topology"));
    await waitFor(() => {
      expect(screen.getByTestId("run-graph-modal")).toBeTruthy();
    });

    fireEvent.keyDown(window, { key: "Tab" });

    const dialog = screen.getByTestId("run-graph-modal");
    expect(dialog.contains(document.activeElement)).toBe(true);
  });
});

describe("GraphInstanceDetail — error handling", () => {
  it("displays load error when getSpec fails", async () => {
    mockGetSpec.mockRejectedValue(new Error("spec not found"));
    renderDetail();
    await waitForDetail();

    await waitFor(() => {
      expect(screen.getByText("spec not found")).toBeTruthy();
    });
  });
});
