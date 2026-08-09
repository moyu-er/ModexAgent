// GraphExecutionViewer.test.tsx — integration tests for the hero view (G05).
//
// Tests how the viewer orchestrates TopologyCanvas, sidebar panels,
// inline deliver panel, and control buttons. useGraphExecution is mocked to return
// controlled state; getSpec is mocked to return YAML that parseGraphSpecYaml
// parses for real (testing the real spec→topology→canvas pipeline).

import { describe, it, expect, vi, afterEach, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent, within } from "@testing-library/react";
import { GraphExecutionViewer } from "./GraphExecutionViewer";
import { useGraphExecution } from "../../hooks/useGraphExecution";
import {
  getSpec,
  deliverToNode,
  pauseGraph,
  resumeGraph,
  stopGraph,
  type GraphInstance,
  type GraphSpecResponse,
} from "../../lib/graphsApi";
import { ToastProvider } from "../ToastContext";

vi.mock("../../hooks/useGraphExecution", () => ({
  useGraphExecution: vi.fn(),
}));

vi.mock("../../lib/graphsApi", () => ({
  getSpec: vi.fn(),
  deliverToNode: vi.fn(),
  pauseGraph: vi.fn(),
  resumeGraph: vi.fn(),
  stopGraph: vi.fn(),
}));

const mockUseGraphExecution = vi.mocked(useGraphExecution);
const mockGetSpec = vi.mocked(getSpec);
const mockDeliverToNode = vi.mocked(deliverToNode);
const mockPauseGraph = vi.mocked(pauseGraph);
const mockResumeGraph = vi.mocked(resumeGraph);
const mockStopGraph = vi.mocked(stopGraph);

// ── Fixtures ─────────────────────────────────────────────────────────────────

const SPEC_YAML = `
name: review_workflow
version: "1.0"
scheduler: parallel
default_trigger: on_receive
nodes:
  - name: designer
    node_type: agent
    config:
      agent: designer
      pool: review
  - name: implementer
    node_type: function
    config: {}
edges:
  - source: __start__
    target: designer
  - source: designer
    target: implementer
  - source: implementer
    target: __end__
`;

const SPEC_RESPONSE: GraphSpecResponse = {
  spec_id: "review_wf",
  name: "review_workflow",
  version: "1.0",
  yaml_content: SPEC_YAML,
};

function makeInstance(overrides: Partial<GraphInstance> = {}): GraphInstance {
  return {
    spec_id: "review_wf",
    graph_instance_id: "12345",
    status: "running",
    nodes: [
      { node_name: "designer", node_id: "node_1", status: "completed", session_id: "abc123.designer" },
      { node_name: "implementer", node_id: "node_2", status: "running" },
    ],
    result: null,
    ...overrides,
  };
}

function mockHook(overrides: {
  instance?: GraphInstance | null;
  timeline?: unknown[];
  pulses?: unknown[];
  crashFlashes?: unknown[];
  error?: string | null;
} = {}): void {
  mockUseGraphExecution.mockReturnValue({
    instance: overrides.instance ?? makeInstance(),
    timeline: (overrides.timeline ?? []) as never,
    pulses: (overrides.pulses ?? []) as never,
    crashFlashes: (overrides.crashFlashes ?? []) as never,
    error: overrides.error ?? null,
    refresh: vi.fn(),
    dismissPulse: vi.fn(),
    dismissCrashFlash: vi.fn(),
  } as never);
}

function renderViewer(props?: {
  workspaceId?: string;
  instanceId?: string;
  onBack?: ReturnType<typeof vi.fn>;
  onJumpToSession?: ReturnType<typeof vi.fn>;
}): void {
  render(
    <ToastProvider>
      <GraphExecutionViewer
        workspaceId={props?.workspaceId ?? ""}
        instanceId={props?.instanceId ?? "12345"}
        onBack={props?.onBack ?? vi.fn()}
        onJumpToSession={props?.onJumpToSession ?? vi.fn()}
      />
    </ToastProvider>,
  );
}

async function waitForCanvas(): Promise<void> {
  await waitFor(() => {
    expect(screen.getByTestId("topology-canvas")).toBeTruthy();
  });
}

// ── Setup / cleanup ─────────────────────────────────────────────────────────

beforeEach(() => {
  mockUseGraphExecution.mockReset();
  mockGetSpec.mockReset();
  mockDeliverToNode.mockReset();
  mockPauseGraph.mockReset();
  mockResumeGraph.mockReset();
  mockStopGraph.mockReset();

  mockHook();
  mockGetSpec.mockResolvedValue(SPEC_RESPONSE);
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

// ── Tests ───────────────────────────────────────────────────────────────────

describe("GraphExecutionViewer — layout", () => {
  it("renders control bar with Back, instance ID, status badge, and control buttons", async () => {
    renderViewer();
    await waitForCanvas();

    const bar = screen.getByTestId("control-bar");
    // Back button
    expect(within(bar).getByText("Back")).toBeTruthy();
    // Instance ID (mono)
    expect(within(bar).getByText("12345")).toBeTruthy();
    // Status badge shows "running"
    expect(within(bar).getByText("running")).toBeTruthy();
    // Control buttons
    expect(within(bar).getByText("Pause")).toBeTruthy();
    expect(within(bar).getByText("Stop")).toBeTruthy();
  });

  it("renders topology canvas, sidebar, and bottom summary bar", async () => {
    renderViewer();
    await waitForCanvas();

    // Canvas
    expect(screen.getByTestId("topology-canvas")).toBeTruthy();
    // Sidebar — instance summary (no node selected)
    expect(screen.getByTestId("sidebar-instance-summary")).toBeTruthy();
    // Progress ring
    expect(screen.getByTestId("progress-ring")).toBeTruthy();
    // Event timeline
    expect(screen.getByTestId("event-timeline")).toBeTruthy();
    // Bottom summary bar: progress text
    const summaryBar = screen.getByTestId("summary-bar");
    expect(within(summaryBar).getByText(/1\/2 nodes/)).toBeTruthy();
    // Scheduler and trigger mode from topology
    expect(within(summaryBar).getByText("parallel")).toBeTruthy();
    expect(within(summaryBar).getByText("on_receive")).toBeTruthy();
  });

  it("renders canvas legend overlay", async () => {
    renderViewer();
    await waitForCanvas();
    expect(screen.getByTestId("graph-canvas-legend")).toBeTruthy();
  });
});

describe("GraphExecutionViewer — node selection", () => {
  it("switches sidebar to NodeDetailPanel when a node is clicked", async () => {
    renderViewer();
    await waitForCanvas();

    // Initially shows instance summary
    expect(screen.getByTestId("sidebar-instance-summary")).toBeTruthy();
    expect(screen.queryByTestId("sidebar-node-detail")).toBeNull();

    // Click the implementer node (function type → single click selects)
    fireEvent.click(screen.getByTestId("graph-node-implementer"));

    // Sidebar switches to node detail
    await waitFor(() => {
      expect(screen.getByTestId("sidebar-node-detail")).toBeTruthy();
    });
    expect(screen.queryByTestId("sidebar-instance-summary")).toBeNull();

    // NodeDetailPanel shows the node name
    expect(screen.getByTestId("node-detail-panel")).toBeTruthy();
  });

  it("does not show Open session button for non-agent nodes in detail panel", async () => {
    renderViewer();
    await waitForCanvas();

    fireEvent.click(screen.getByTestId("graph-node-implementer"));

    await waitFor(() => {
      expect(screen.getByTestId("node-detail-panel")).toBeTruthy();
    });

    // function node → no "Open session transcript" button
    const panel = screen.getByTestId("node-detail-panel");
    expect(within(panel).queryByText("Open session transcript")).toBeNull();
  });

  it("calls onJumpToSession with node.session_id when agent node is clicked", async () => {
    const onJumpToSession = vi.fn();
    renderViewer({ onJumpToSession });
    await waitForCanvas();

    // Agent node: single click → direct jump to session
    fireEvent.click(screen.getByTestId("graph-node-designer"));

    expect(onJumpToSession).toHaveBeenCalledWith("abc123.designer");
  });
});

describe("GraphExecutionViewer — control button state machine", () => {
  it("shows Pause and Stop when running (no Resume)", async () => {
    mockHook({ instance: makeInstance({ status: "running" }) });
    renderViewer();
    await waitForCanvas();

    const bar = screen.getByTestId("control-bar");
    expect(within(bar).getByText("Pause")).toBeTruthy();
    expect(within(bar).getByText("Stop")).toBeTruthy();
    expect(within(bar).queryByText("Resume")).toBeNull();
  });

  it("shows Resume and Stop when paused (no Pause)", async () => {
    mockHook({
      instance: makeInstance({
        status: "paused",
        nodes: [
          { node_name: "designer", node_id: "node_1", status: "completed" },
          { node_name: "implementer", node_id: "node_2", status: "pending" },
        ],
      }),
    });
    renderViewer();
    await waitForCanvas();

    const bar = screen.getByTestId("control-bar");
    expect(within(bar).getByText("Resume")).toBeTruthy();
    expect(within(bar).getByText("Stop")).toBeTruthy();
    expect(within(bar).queryByText("Pause")).toBeNull();
  });

  it("hides all control buttons when completed (terminal)", async () => {
    mockHook({
      instance: makeInstance({
        status: "completed",
        nodes: [
          { node_name: "designer", node_id: "node_1", status: "completed" },
          { node_name: "implementer", node_id: "node_2", status: "completed" },
        ],
        result: [{ content: "done" }],
      }),
    });
    renderViewer();
    await waitForCanvas();

    const bar = screen.getByTestId("control-bar");
    expect(within(bar).queryByText("Pause")).toBeNull();
    expect(within(bar).queryByText("Resume")).toBeNull();
    expect(within(bar).queryByText("Stop")).toBeNull();
  });

  it("calls pauseGraph when Pause is clicked", async () => {
    mockHook({ instance: makeInstance({ status: "running" }) });
    renderViewer();
    await waitForCanvas();

    const bar = screen.getByTestId("control-bar");
    fireEvent.click(within(bar).getByText("Pause"));

    await waitFor(() => {
      expect(mockPauseGraph).toHaveBeenCalledWith("", "12345");
    });
  });
});

describe("GraphExecutionViewer — inline deliver panel", () => {
  it("renders inline deliver panel when running", async () => {
    mockHook({ instance: makeInstance({ status: "running" }) });
    renderViewer();
    await waitForCanvas();

    await waitFor(() => {
      expect(screen.getByTestId("deliver-inline-panel")).toBeTruthy();
    });
  });

  it("renders inline deliver panel when paused", async () => {
    mockHook({
      instance: makeInstance({
        status: "paused",
        nodes: [
          { node_name: "designer", node_id: "node_1", status: "completed" },
          { node_name: "implementer", node_id: "node_2", status: "pending" },
        ],
      }),
    });
    renderViewer();
    await waitForCanvas();

    await waitFor(() => {
      expect(screen.getByTestId("deliver-inline-panel")).toBeTruthy();
    });
  });

  it.each(["completed", "crashed", "stopped"])(
    "hides inline deliver panel when terminal (%s)",
    async (status) => {
      mockHook({
        instance: makeInstance({
          status,
          nodes: [
            { node_name: "designer", node_id: "node_1", status: "completed" },
            { node_name: "implementer", node_id: "node_2", status: "completed" },
          ],
        }),
      });
      renderViewer();
      await waitForCanvas();

      expect(screen.queryByTestId("deliver-inline-panel")).toBeNull();
    },
  );

  it("Send button disabled when content is empty", async () => {
    mockHook({ instance: makeInstance({ status: "running" }) });
    renderViewer();
    await waitForCanvas();

    await waitFor(() => {
      expect(screen.getByTestId("deliver-inline-panel")).toBeTruthy();
    });

    const panel = screen.getByTestId("deliver-inline-panel");
    const sendButton = within(panel).getByRole("button", { name: "Deliver" }) as HTMLButtonElement;
    expect(sendButton.disabled).toBe(true);
  });

  it("calls deliverToNode when content is entered and Send is clicked", async () => {
    mockHook({ instance: makeInstance({ status: "running" }) });
    renderViewer();
    await waitForCanvas();

    await waitFor(() => {
      expect(screen.getByTestId("deliver-inline-panel")).toBeTruthy();
    });

    const panel = screen.getByTestId("deliver-inline-panel");

    // Enter content in the textarea
    const textarea = panel.querySelector("textarea");
    expect(textarea).toBeTruthy();
    fireEvent.change(textarea!, { target: { value: "hello world" } });

    const sendButton = within(panel).getByRole("button", { name: "Deliver" }) as HTMLButtonElement;
    expect(sendButton.disabled).toBe(false);
    fireEvent.click(sendButton);

    await waitFor(() => {
      expect(mockDeliverToNode).toHaveBeenCalledWith(
        "",
        "12345",
        "designer",
        "hello world",
      );
    });
  });

  it("clears content after successful deliver", async () => {
    mockHook({ instance: makeInstance({ status: "running" }) });
    renderViewer();
    await waitForCanvas();

    await waitFor(() => {
      expect(screen.getByTestId("deliver-inline-panel")).toBeTruthy();
    });

    const panel = screen.getByTestId("deliver-inline-panel");
    const textarea = panel.querySelector("textarea") as HTMLTextAreaElement;
    fireEvent.change(textarea, { target: { value: "hello world" } });

    const sendButton = within(panel).getByRole("button", { name: "Deliver" });
    fireEvent.click(sendButton);

    await waitFor(() => {
      expect(mockDeliverToNode).toHaveBeenCalled();
    });

    await waitFor(() => {
      expect(textarea.value).toBe("");
    });
  });
});

describe("GraphExecutionViewer — graph-level result", () => {
  it("shows result in InstanceSummary when instance is completed", async () => {
    mockHook({
      instance: makeInstance({
        status: "completed",
        nodes: [
          { node_name: "designer", node_id: "node_1", status: "completed" },
          { node_name: "implementer", node_id: "node_2", status: "completed" },
        ],
        result: [{ content: "All done, ship it!" }],
      }),
    });
    renderViewer();
    await waitForCanvas();

    // Instance summary is shown (no node selected)
    expect(screen.getByTestId("instance-summary")).toBeTruthy();

    // Result content is displayed
    expect(screen.getByText("All done, ship it!")).toBeTruthy();
  });

  it("shows no result message when completed with null result", async () => {
    mockHook({
      instance: makeInstance({
        status: "completed",
        nodes: [
          { node_name: "designer", node_id: "node_1", status: "completed" },
          { node_name: "implementer", node_id: "node_2", status: "completed" },
        ],
        result: null,
      }),
    });
    renderViewer();
    await waitForCanvas();

    expect(screen.getByText("No result")).toBeTruthy();
  });
});

describe("GraphExecutionViewer — event timeline", () => {
  it("renders timeline events with kind labels", async () => {
    mockHook({
      instance: makeInstance(),
      timeline: [
        {
          key: "derived:node_1:completed:1000",
          kind: "node_completed",
          timestamp: 1000,
          derived: true,
          nodeId: "node_1",
          nodeName: "designer",
        },
        {
          key: "derived:node_2:running:2000",
          kind: "node_started",
          timestamp: 2000,
          derived: true,
          nodeId: "node_2",
          nodeName: "implementer",
        },
      ],
    });
    renderViewer();
    await waitForCanvas();

    const timeline = screen.getByTestId("event-timeline");
    expect(within(timeline).getByText("node_completed")).toBeTruthy();
    expect(within(timeline).getByText("node_started")).toBeTruthy();
  });

  it("shows empty state when no events", async () => {
    mockHook({ instance: makeInstance(), timeline: [] });
    renderViewer();
    await waitForCanvas();

    expect(screen.getByText("No events yet")).toBeTruthy();
  });

  it("marks derived events as inferred", async () => {
    mockHook({
      instance: makeInstance(),
      timeline: [
        {
          key: "derived:node_1:completed:1000",
          kind: "node_completed",
          timestamp: 1000,
          derived: true,
          nodeId: "node_1",
          nodeName: "designer",
        },
      ],
    });
    renderViewer();
    await waitForCanvas();

    expect(screen.getByText("(inferred)")).toBeTruthy();
  });
});

describe("GraphExecutionViewer — back button", () => {
  it("calls onBack when Back button is clicked", async () => {
    const onBack = vi.fn();
    renderViewer({ onBack });
    await waitForCanvas();

    fireEvent.click(screen.getByText("Back"));
    expect(onBack).toHaveBeenCalledOnce();
  });
});
