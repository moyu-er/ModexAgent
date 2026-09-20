// WorkspacePod PA-02 wiring — the one rename dialog shared by both entries,
// title propagation to the chat header, sessions_changed refresh, and
// preservation of the active editor/chat state across a rename.

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor, act } from "@testing-library/react";
import { WorkspacePod } from "./WorkspacePod";
import { ToastProvider } from "./ToastContext";
import {
  fetchSessions,
  renameSessionTitle,
  ApiError,
} from "../lib/api";

vi.mock("../lib/api", () => ({
  fetchSessions: vi.fn(),
  fetchPools: vi.fn(),
  deleteConversation: vi.fn(),
  renameSessionTitle: vi.fn(),
  fetchMessages: vi.fn().mockResolvedValue([]),
  fetchTodos: vi.fn().mockResolvedValue([]),
  fetchApprovals: vi.fn().mockResolvedValue([]),
  submitApproval: vi.fn(),
  fetchModels: vi.fn().mockResolvedValue({ choices: [] }),
  fetchMediaConfig: vi.fn(),
  uploadAttachment: vi.fn(),
  attachmentDownloadUrl: (sid: string, id: string, ws?: string) =>
    `/api/sessions/${sid}/attachments/${id}${ws ? `?ws=${ws}` : ""}`,
  ApiError: class ApiError extends Error {
    constructor(
      readonly status: number,
      readonly statusText: string,
      readonly detail: string,
    ) {
      super(`API ${status} ${statusText}${detail ? `: ${detail}` : ""}`);
      this.name = "ApiError";
    }
  },
}));

vi.mock("../lib/timezone", () => ({
  setTimezone: vi.fn(),
  formatShort: vi.fn(() => "12:00"),
  formatClock: vi.fn(() => "12:00"),
}));

class FakeWebSocket {
  static readonly OPEN = 1;
  readyState = FakeWebSocket.OPEN;
  onopen: ((ev: Event) => void) | null = null;
  onclose: ((ev: CloseEvent) => void) | null = null;
  onmessage: ((ev: MessageEvent) => void) | null = null;
  onerror: ((ev: Event) => void) | null = null;
  sent: { action?: string; pool?: string }[] = [];
  constructor() {
    queueMicrotask(() => {
      this.onopen?.(new Event("open"));
    });
  }
  send(data: string): void {
    this.sent.push(JSON.parse(data));
  }
  close(): void {
    this.readyState = 3;
  }
  dispatchEvent(): boolean {
    return true;
  }
}

let sockets: FakeWebSocket[] = [];

const baseProps = {
  tabId: "__home__",
  workspacePath: "F:\\home\\project",
  scopeWs: "",
  active: true,
  route: { kind: "chat" } as const,
  navigate: vi.fn(),
  pools: [{ name: "main" }],
  poolAgentMap: { main: "main" },
  sidebarWidth: 260,
  resizing: false,
  onResizeMouseDown: vi.fn(),
  mobileOpen: false,
  onCloseMobile: vi.fn(),
  onOpenMobile: vi.fn(),
  onReportStatus: vi.fn(),
};

function makeSession(overrides: Record<string, unknown> = {}) {
  return {
    session_id: "abc123.main",
    agent_name: "main",
    pool: "main",
    parent_session_id: null,
    created_at: 1,
    updated_at: 1,
    ...overrides,
  };
}

describe("WorkspacePod rename wiring (PA-02)", () => {
  beforeEach(() => {
    sockets = [];
    vi.stubGlobal(
      "WebSocket",
      class extends FakeWebSocket {
        constructor() {
          super();
          sockets.push(this);
        }
      },
    );
    vi.mocked(fetchSessions).mockResolvedValue([makeSession()]);
    vi.mocked(renameSessionTitle).mockResolvedValue({ updated: true });
    window.location.hash = "";
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.clearAllMocks();
    localStorage.clear();
  });

  it("sidebar rename entry opens the shared dialog and saves via PATCH + refresh", async () => {
    const { rerender } = render(<ToastProvider><WorkspacePod {...baseProps} /></ToastProvider>);
    await waitFor(() =>
      expect(screen.getByText("abc123.main")).toBeTruthy(),
    );

    fireEvent.click(screen.getByRole("button", { name: "Rename conversation" }));
    const input = screen.getByTestId("rename-input") as HTMLInputElement;
    // Untitled session → empty input, full id placeholder.
    expect(input.value).toBe("");
    expect(input.placeholder).toBe("abc123.main");

    vi.mocked(fetchSessions).mockResolvedValue([
      makeSession({ metadata: { title: "Trip plan" } }),
    ]);
    fireEvent.change(input, { target: { value: "Trip plan" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() =>
      expect(renameSessionTitle).toHaveBeenCalledWith(
        "abc123.main",
        "Trip plan",
        undefined,
        "main",
      ),
    );
    await waitFor(() => expect(screen.getByText("Trip plan")).toBeTruthy());
    // Dialog closed after successful save.
    await waitFor(() =>
      expect(screen.queryByRole("dialog")).toBeNull(),
    );
    void rerender;
  });

  it("header ··· menu opens the SAME dialog for the selected session", async () => {
    render(<ToastProvider><WorkspacePod {...baseProps} /></ToastProvider>);
    await waitFor(() =>
      expect(screen.getByText("abc123.main")).toBeTruthy(),
    );
    fireEvent.click(screen.getByText("abc123.main"));
    await waitFor(() =>
      expect(screen.getByTestId("chat-header-title")).toBeTruthy(),
    );

    fireEvent.click(screen.getByRole("button", { name: "Conversation menu" }));
    fireEvent.click(screen.getByRole("menuitem", { name: "Rename" }));

    await waitFor(() =>
      expect(screen.getByTestId("rename-input")).toBeTruthy(),
    );
    expect((screen.getByTestId("rename-input") as HTMLInputElement).placeholder).toBe(
      "abc123.main",
    );
  });

  it("header shows the title; whitespace title falls back to the id", async () => {
    vi.mocked(fetchSessions).mockResolvedValue([
      makeSession({ metadata: { title: "   " } }),
    ]);
    render(<ToastProvider><WorkspacePod {...baseProps} /></ToastProvider>);
    await waitFor(() =>
      expect(screen.getByText("abc123.main")).toBeTruthy(),
    );
    fireEvent.click(screen.getByText("abc123.main"));
    await waitFor(() =>
      expect(screen.getByTestId("chat-header-title").textContent).toBe("abc123.main"),
    );
  });

  it("failed rename keeps the dialog open with the user's text", async () => {
    vi.mocked(renameSessionTitle).mockRejectedValue(
      new ApiError(404, "Not Found", "no such session"),
    );
    render(<ToastProvider><WorkspacePod {...baseProps} /></ToastProvider>);
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Rename conversation" })).toBeTruthy(),
    );

    fireEvent.click(screen.getByRole("button", { name: "Rename conversation" }));
    fireEvent.change(screen.getByTestId("rename-input"), { target: { value: "Keep" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() =>
      expect(screen.getByText("Could not save the title.")).toBeTruthy(),
    );
    expect((screen.getByTestId("rename-input") as HTMLInputElement).value).toBe("Keep");
    expect(screen.getByRole("dialog")).toBeTruthy();
  });

  it("sessions_changed WS ping refreshes the sidebar (own workspace only)", async () => {
    render(<ToastProvider><WorkspacePod {...baseProps} /></ToastProvider>);
    await waitFor(() =>
      expect(screen.getByText("abc123.main")).toBeTruthy(),
    );

    vi.mocked(fetchSessions).mockResolvedValue([
      makeSession({ metadata: { title: "Auto title" } }),
    ]);
    const wsSocket = sockets[0]!;
    act(() => {
      wsSocket.onmessage?.({
        data: JSON.stringify({ type: "sessions_changed", workspace: "" }),
      } as MessageEvent);
    });

    await waitFor(() => expect(screen.getByText("Auto title")).toBeTruthy());
    // The control ping produced no chat transcript entries.
    expect(screen.queryByText("sessions_changed")).toBeNull();
  });

  it("cross-workspace ping does not refresh this pod's list", async () => {
    render(<ToastProvider><WorkspacePod {...baseProps} /></ToastProvider>);
    await waitFor(() =>
      expect(screen.getByText("abc123.main")).toBeTruthy(),
    );
    const callsBefore = vi.mocked(fetchSessions).mock.calls.length;

    vi.mocked(fetchSessions).mockResolvedValue([
      makeSession({ metadata: { title: "Other" } }),
    ]);
    act(() => {
      sockets[0]!.onmessage?.({
        data: JSON.stringify({ type: "sessions_changed", workspace: "/ws/other" }),
      } as MessageEvent);
    });
    expect(vi.mocked(fetchSessions).mock.calls.length).toBe(callsBefore);
    expect(screen.queryByText("Other")).toBeNull();
  });

  it("preserves the selected session + composer presence across a rename refresh", async () => {
    const { rerender } = render(<ToastProvider><WorkspacePod {...baseProps} /></ToastProvider>);
    // Select the session (sidebar row click).
    await waitFor(() =>
      expect(screen.getByText("abc123.main")).toBeTruthy(),
    );
    fireEvent.click(screen.getByText("abc123.main"));

    vi.mocked(fetchSessions).mockResolvedValue([
      makeSession({ metadata: { title: "Renamed" }, updated_at: 99 }),
    ]);
    act(() => {
      sockets[0]!.onmessage?.({
        data: JSON.stringify({ type: "sessions_changed", workspace: "" }),
      } as MessageEvent);
    });

    // Still on the same session's chat view (header present, composer there).
    await waitFor(() =>
      expect(screen.getByTestId("chat-header-title").textContent).toBe("Renamed"),
    );
    expect(document.querySelector("form.composer")).toBeTruthy();
    void rerender;
  });

  it("upper-left selector resets the hero hint and drives the new conversation's pool", async () => {
    vi.mocked(fetchSessions).mockResolvedValue([]);
    render(
      <ToastProvider>
        <WorkspacePod {...baseProps} pools={[{ name: "main" }, { name: "coder" }]} />
      </ToastProvider>,
    );
    // localStorage was cleared and no preferred pool is passed → nothing is
    // silently selected; the hero shows the first rotating hint.
    await waitFor(() =>
      expect(screen.getByText("What can I help you build?")).toBeTruthy(),
    );

    // Pick "coder" in the UPPER-LEFT sidebar selector.
    fireEvent.click(screen.getByLabelText("Agent pool"));
    fireEvent.click(screen.getByRole("option", { name: "coder" }));

    // The keyed hero group remounts for the new pool — the hint rotation
    // restarts at the first hint.
    expect(screen.getByText("What can I help you build?")).toBeTruthy();

    // Sending from the hero composer attaches the draft to "coder".
    fireEvent.change(screen.getByPlaceholderText("Message…"), {
      target: { value: "hello coder" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    await waitFor(() => {
      const attach = sockets[0]!.sent.find((m) => m.action === "attach");
      expect(attach?.pool).toBe("coder");
    });
  });

  it("existing session keeps its own pool across sidebar selector changes", async () => {
    render(
      <ToastProvider>
        <WorkspacePod {...baseProps} pools={[{ name: "main" }, { name: "coder" }]} />
      </ToastProvider>,
    );
    await waitFor(() =>
      expect(screen.getByText("abc123.main")).toBeTruthy(),
    );
    fireEvent.click(screen.getByText("abc123.main"));
    await waitFor(() =>
      expect(screen.getByTestId("chat-header-title")).toBeTruthy(),
    );

    // Change the sidebar selection — the open chat must stay put.
    fireEvent.click(screen.getByLabelText("Agent pool"));
    fireEvent.click(screen.getByRole("option", { name: "coder" }));
    expect(screen.getByTestId("chat-header-title").textContent).toBe("abc123.main");
    expect(document.querySelector("form.composer")).toBeTruthy();
  });

  it("hero send is disabled and directs to the top-left selector when no pool is selected", async () => {
    vi.mocked(fetchSessions).mockResolvedValue([]);
    render(
      <ToastProvider>
        <WorkspacePod {...baseProps} pools={[{ name: "main" }, { name: "coder" }]} />
      </ToastProvider>,
    );
    await waitFor(() =>
      expect(screen.getByText("What can I help you build?")).toBeTruthy(),
    );
    // No pool selected → composer gate: typing + Enter does nothing.
    const ta = screen.getByPlaceholderText("Message…") as HTMLTextAreaElement;
    fireEvent.change(ta, { target: { value: "blocked" } });
    fireEvent.keyDown(ta, { key: "Enter" });
    expect(
      sockets[0]!.sent.filter((m) => m.action === "attach"),
    ).toHaveLength(0);
    expect(
      screen.getByText("Choose an assistant at the top left to start a conversation."),
    ).toBeTruthy();
    // The sidebar's top-left selector is right there.
    expect(screen.getByLabelText("Agent pool")).toBeTruthy();
  });
});
