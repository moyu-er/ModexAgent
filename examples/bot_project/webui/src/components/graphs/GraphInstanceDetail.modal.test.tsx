// GraphInstanceDetail.modal.test.tsx — integration tests for the Run Graph
// modal (migrated from the retired full-page execution viewer test suite).
//
// The modal carries the full live-graph experience: top control bar
// (spec name · version chip · status badge · Pause/Resume/Stop), full-size
// TopologyCanvas, context sidebar (NodeDetailPanel / InstanceSummary +
// EventTimeline), and the inline deliver panel. useGraphExecution is mocked
// to return controlled state; getSpec is mocked to return YAML that
// parseGraphSpecYaml parses for real (testing the real spec→topology→canvas
// pipeline). Every test opens the modal via the header "Topology" button.

import { describe, it, expect, vi, afterEach, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent, within, act } from "@testing-library/react";
import { GraphInstanceDetail } from "./GraphInstanceDetail";
import { useGraphExecution } from "../../hooks/useGraphExecution";
import {
  getSpec,
  getInvocations,
  invokeInstance,
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
  getInvocations: vi.fn(),
  invokeInstance: vi.fn(),
  deliverToNode: vi.fn(),
  pauseGraph: vi.fn(),
  resumeGraph: vi.fn(),
  stopGraph: vi.fn(),
}));

const mockUseGraphExecution = vi.mocked(useGraphExecution);
const mockGetSpec = vi.mocked(getSpec);
const mockGetInvocations = vi.mocked(getInvocations);
const mockInvokeInstance = vi.mocked(invokeInstance);
const mockDeliverToNode = vi.mocked(deliverToNode);
const mockPauseGraph = vi.mocked(pauseGraph);
const mockResumeGraph = vi.mocked(resumeGraph);
const mockStopGraph = vi.mocked(stopGraph);

// ── Fixtures ─────────────────────────────────────────────────────────────────

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
  dismissCrashFlash?: (id: number) => void;
} = {}): void {
  mockUseGraphExecution.mockReturnValue({
    instance: overrides.instance ?? makeInstance(),
    timeline: (overrides.timeline ?? []) as never,
    pulses: (overrides.pulses ?? []) as never,
    crashFlashes: (overrides.crashFlashes ?? []) as never,
    error: overrides.error ?? null,
    refresh: vi.fn(),
    dismissPulse: vi.fn(),
    dismissCrashFlash: overrides.dismissCrashFlash ?? vi.fn(),
  } as never);
}

function renderDetail(props?: {
  workspaceId?: string;
  instanceId?: string;
  onBack?: ReturnType<typeof vi.fn>;
  onJumpToSession?: ReturnType<typeof vi.fn>;
}) {
  return render(
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

async function waitForDialog(): Promise<HTMLElement> {
  await waitFor(() => {
    expect(screen.getByTestId("run-graph-modal")).toBeTruthy();
  });
  return screen.getByTestId("run-graph-modal");
}

async function openModal(props?: Parameters<typeof renderDetail>[0]): Promise<HTMLElement> {
  renderDetail(props);
  await waitFor(() => {
    expect(screen.getByTestId("graph-instance-detail")).toBeTruthy();
  });
  fireEvent.click(screen.getByText("Topology"));
  const dialog = await waitForDialog();
  await waitFor(() => {
    expect(within(dialog).getByTestId("topology-canvas")).toBeTruthy();
  });
  return dialog;
}

// ── Setup / cleanup ─────────────────────────────────────────────────────────

beforeEach(() => {
  mockUseGraphExecution.mockReset();
  mockGetSpec.mockReset();
  mockGetInvocations.mockReset();
  mockInvokeInstance.mockReset();
  mockDeliverToNode.mockReset();
  mockPauseGraph.mockReset();
  mockResumeGraph.mockReset();
  mockStopGraph.mockReset();

  mockHook();
  mockGetSpec.mockResolvedValue(SPEC_RESPONSE);
  mockGetInvocations.mockResolvedValue([]);
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

// ── Tests ───────────────────────────────────────────────────────────────────

describe("Run graph modal — layout", () => {
  it("renders top bar with spec name, version chip, status badge, and control buttons", async () => {
    const dialog = await openModal();

    const bar = within(dialog).getByTestId("control-bar");
    // Spec name + version chip
    expect(within(bar).getByText("review_workflow")).toBeTruthy();
    expect(within(bar).getByText("spec v1.0")).toBeTruthy();
    // Status badge shows "running"
    expect(within(bar).getByText("running")).toBeTruthy();
    // Control buttons
    expect(within(bar).getByText("Pause")).toBeTruthy();
    expect(within(bar).getByText("Stop")).toBeTruthy();
  });

  it("renders topology canvas, sidebar with instance summary, and event timeline", async () => {
    const dialog = await openModal();

    // Canvas
    expect(within(dialog).getByTestId("topology-canvas")).toBeTruthy();
    // Sidebar — instance summary (no node selected)
    expect(within(dialog).getByTestId("sidebar-instance-summary")).toBeTruthy();
    // Progress ring
    expect(within(dialog).getByTestId("progress-ring")).toBeTruthy();
    // Event timeline
    expect(within(dialog).getByTestId("event-timeline")).toBeTruthy();
    // Instance summary shows scheduler + trigger mode from topology
    const summary = within(dialog).getByTestId("instance-summary");
    expect(within(summary).getByText("parallel")).toBeTruthy();
    expect(within(summary).getByText("on_all_preds")).toBeTruthy();
  });

  it("renders canvas legend overlay", async () => {
    const dialog = await openModal();
    expect(within(dialog).getByTestId("graph-canvas-legend")).toBeTruthy();
  });
});

describe("Run graph modal — node selection", () => {
  it("switches sidebar to NodeDetailPanel when a node is clicked", async () => {
    const dialog = await openModal();

    // Initially shows instance summary
    expect(within(dialog).getByTestId("sidebar-instance-summary")).toBeTruthy();
    expect(within(dialog).queryByTestId("sidebar-node-detail")).toBeNull();

    // Click the implementer node (function type → single click selects)
    fireEvent.click(within(dialog).getByTestId("graph-node-implementer"));

    // Sidebar switches to node detail
    await waitFor(() => {
      expect(within(dialog).getByTestId("sidebar-node-detail")).toBeTruthy();
    });
    expect(within(dialog).queryByTestId("sidebar-instance-summary")).toBeNull();

    // NodeDetailPanel shows the node name
    expect(within(dialog).getByTestId("node-detail-panel")).toBeTruthy();
  });

  it("does not show Open session button for non-agent nodes in detail panel", async () => {
    const dialog = await openModal();

    fireEvent.click(within(dialog).getByTestId("graph-node-implementer"));

    await waitFor(() => {
      expect(within(dialog).getByTestId("node-detail-panel")).toBeTruthy();
    });

    // function node → no "Open session transcript" button
    const panel = within(dialog).getByTestId("node-detail-panel");
    expect(within(panel).queryByText("Open session transcript")).toBeNull();
  });

  it("calls onJumpToSession with node.session_id when agent node is clicked", async () => {
    const onJumpToSession = vi.fn();
    const dialog = await openModal({ onJumpToSession });

    // Agent node: single click → direct jump to session
    fireEvent.click(within(dialog).getByTestId("graph-node-designer"));

    expect(onJumpToSession).toHaveBeenCalledWith("abc123.designer");
  });
});

describe("Run graph modal — control button state machine", () => {
  it("shows Pause and Stop when running (no Resume)", async () => {
    mockHook({ instance: makeInstance({ status: "running" }) });
    const dialog = await openModal();

    const bar = within(dialog).getByTestId("control-bar");
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
    const dialog = await openModal();

    const bar = within(dialog).getByTestId("control-bar");
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
    const dialog = await openModal();

    const bar = within(dialog).getByTestId("control-bar");
    expect(within(bar).queryByText("Pause")).toBeNull();
    expect(within(bar).queryByText("Resume")).toBeNull();
    expect(within(bar).queryByText("Stop")).toBeNull();
  });

  it("calls pauseGraph when Pause is clicked", async () => {
    mockHook({ instance: makeInstance({ status: "running" }) });
    const dialog = await openModal();

    const bar = within(dialog).getByTestId("control-bar");
    fireEvent.click(within(bar).getByText("Pause"));

    await waitFor(() => {
      expect(mockPauseGraph).toHaveBeenCalledWith("", "12345");
    });
  });
});

describe("Run graph modal — inline deliver panel", () => {
  it("renders inline deliver panel when running", async () => {
    mockHook({ instance: makeInstance({ status: "running" }) });
    const dialog = await openModal();

    await waitFor(() => {
      expect(within(dialog).getByTestId("deliver-inline-panel")).toBeTruthy();
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
    const dialog = await openModal();

    await waitFor(() => {
      expect(within(dialog).getByTestId("deliver-inline-panel")).toBeTruthy();
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
      const dialog = await openModal();

      expect(within(dialog).queryByTestId("deliver-inline-panel")).toBeNull();
    },
  );

  it("Send button disabled when content is empty", async () => {
    mockHook({ instance: makeInstance({ status: "running" }) });
    const dialog = await openModal();

    await waitFor(() => {
      expect(within(dialog).getByTestId("deliver-inline-panel")).toBeTruthy();
    });

    const panel = within(dialog).getByTestId("deliver-inline-panel");
    const sendButton = within(panel).getByRole("button", { name: "Deliver" }) as HTMLButtonElement;
    expect(sendButton.disabled).toBe(true);
  });

  it("calls deliverToNode when content is entered and Send is clicked", async () => {
    mockHook({ instance: makeInstance({ status: "running" }) });
    const dialog = await openModal();

    await waitFor(() => {
      expect(within(dialog).getByTestId("deliver-inline-panel")).toBeTruthy();
    });

    const panel = within(dialog).getByTestId("deliver-inline-panel");

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
    const dialog = await openModal();

    await waitFor(() => {
      expect(within(dialog).getByTestId("deliver-inline-panel")).toBeTruthy();
    });

    const panel = within(dialog).getByTestId("deliver-inline-panel");
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

describe("Run graph modal — graph-level result", () => {
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
    const dialog = await openModal();

    // Instance summary is shown (no node selected)
    expect(within(dialog).getByTestId("instance-summary")).toBeTruthy();

    // Result content is displayed
    expect(within(dialog).getByText("All done, ship it!")).toBeTruthy();
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
    const dialog = await openModal();

    expect(within(dialog).getByText("No result")).toBeTruthy();
  });
});

describe("Run graph modal — event timeline", () => {
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
    const dialog = await openModal();

    const timeline = within(dialog).getByTestId("event-timeline");
    expect(within(timeline).getByText("node_completed")).toBeTruthy();
    expect(within(timeline).getByText("node_started")).toBeTruthy();
  });

  it("shows empty state when no events", async () => {
    mockHook({ instance: makeInstance(), timeline: [] });
    const dialog = await openModal();

    expect(within(dialog).getByText("No events yet")).toBeTruthy();
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
    const dialog = await openModal();

    expect(within(dialog).getByText("(inferred)")).toBeTruthy();
  });
});

describe("Run graph modal — crash flash (§8.1)", () => {
  const FLASH = {
    id: 1,
    nodeId: "node_1",
    nodeName: "designer",
    timestamp: 1000,
  };

  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("renders a crash-flash outline on the flashed node and dismisses it after 220ms", async () => {
    const dismissCrashFlash = vi.fn();
    mockHook({ crashFlashes: [FLASH], dismissCrashFlash });
    renderDetail();
    // Flush the async spec/invocation loads without waitFor (fake timers).
    await act(async () => {});
    fireEvent.click(screen.getByText("Topology"));

    const canvas = screen.getByTestId("topology-canvas");
    expect(canvas.querySelectorAll("[data-crash-flash]")).toHaveLength(1);

    act(() => {
      vi.advanceTimersByTime(220);
    });
    expect(dismissCrashFlash).toHaveBeenCalledWith(FLASH.id);
  });

  it("auto-dismisses crash flashes after 220ms even while the modal is closed", async () => {
    const dismissCrashFlash = vi.fn();
    mockHook({ crashFlashes: [FLASH], dismissCrashFlash });
    renderDetail();
    await act(async () => {});

    expect(screen.queryByTestId("run-graph-modal")).toBeNull();
    expect(dismissCrashFlash).not.toHaveBeenCalled();

    act(() => {
      vi.advanceTimersByTime(220);
    });
    expect(dismissCrashFlash).toHaveBeenCalledWith(FLASH.id);
  });

  it("clears pending crash-flash timers on unmount", async () => {
    const dismissCrashFlash = vi.fn();
    mockHook({ crashFlashes: [FLASH], dismissCrashFlash });
    const view = renderDetail();
    await act(async () => {});

    view.unmount();
    act(() => {
      vi.advanceTimersByTime(500);
    });
    expect(dismissCrashFlash).not.toHaveBeenCalled();
  });
});

