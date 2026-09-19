// ChatView header (PA-02): shows the shared session display title and a
// `···` menu whose Rename entry opens the one shared rename dialog. The
// agentName remains visible. Menu opens/closes; rename invokes the callback
// with the selected session id.

import { describe, it, expect, vi, afterEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { ChatView } from "./ChatView";
import type { UIMessage } from "../types/events";

vi.mock("../lib/api", () => ({
  fetchMediaConfig: vi.fn(),
  uploadAttachment: vi.fn(),
  fetchModels: vi.fn().mockResolvedValue({ choices: [] }),
}));

const baseProps = {
  messages: [] as UIMessage[],
  isStreaming: false,
  isPending: false,
  todos: [],
  pendingApprovals: [],
  isApprovingBatch: false,
  submitApproval: vi.fn(),
  onApproveAll: vi.fn(),
  onSend: vi.fn(),
  sessionId: "abc123.main" as string | null,
  agentName: "main",
};

afterEach(() => {
  vi.clearAllMocks();
});

describe("ChatView header title (PA-02)", () => {
  it("shows the session title via the shared display function", () => {
    render(<ChatView {...baseProps} sessionTitle="Trip plan" />);
    expect(screen.getByText("Trip plan")).toBeTruthy();
  });

  it("shows the FULL session id when there is no title", () => {
    render(<ChatView {...baseProps} />);
    expect(screen.getByText("abc123.main")).toBeTruthy();
  });

  it("falls back to the session id for a whitespace-only title", () => {
    render(<ChatView {...baseProps} sessionTitle="   " />);
    expect(screen.getByText("abc123.main")).toBeTruthy();
  });

  it("header does not render the title on the hero view (no session)", () => {
    render(<ChatView {...baseProps} sessionId={null} sessionTitle="Nope" />);
    expect(screen.queryByText("Nope")).toBeNull();
  });
});

describe("ChatView header menu (PA-02)", () => {
  it("opens the ··· menu and Rename calls onRenameSession", () => {
    const onRenameSession = vi.fn();
    render(<ChatView {...baseProps} onRenameSession={onRenameSession} />);

    fireEvent.click(screen.getByRole("button", { name: "Conversation menu" }));
    fireEvent.click(screen.getByRole("menuitem", { name: "Rename" }));
    expect(onRenameSession).toHaveBeenCalledTimes(1);
  });

  it("hides the rename entry when no session is selected or callback missing", () => {
    const { rerender } = render(
      <ChatView {...baseProps} sessionId={null} onRenameSession={vi.fn()} />,
    );
    expect(screen.queryByRole("button", { name: "Conversation menu" })).toBeNull();
    rerender(<ChatView {...baseProps} />);
    expect(screen.queryByRole("button", { name: "Conversation menu" })).toBeNull();
  });
});
