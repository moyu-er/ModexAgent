import { describe, it, expect, vi } from "vitest";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { ChatView } from "./ChatView";
import type { UIMessage } from "../types/events";

vi.mock("../lib/api", () => ({
  fetchMediaConfig: vi.fn(),
  uploadAttachment: vi.fn(),
  fetchModels: vi.fn().mockResolvedValue({ choices: [] }),
  attachmentDownloadUrl: (sid: string, id: string, ws?: string) =>
    `/api/sessions/${sid}/attachments/${id}${ws ? `?ws=${ws}` : ""}`,
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
};

describe("ChatView hero view (no session selected)", () => {
  it("requires a selected pool when canStart is false (no hero picker)", () => {
    const onHeroSend = vi.fn();
    render(<ChatView {...baseProps} sessionId={null} pool="" canStart={false} onHeroSend={onHeroSend} />);
    fireEvent.change(screen.getByPlaceholderText("Message…"), { target: { value: "Keep this draft" } });
    fireEvent.keyDown(screen.getByPlaceholderText("Message…"), { key: "Enter" });
    expect(onHeroSend).not.toHaveBeenCalled();
    expect((screen.getByPlaceholderText("Message…") as HTMLTextAreaElement).value).toBe("Keep this draft");
    // Directs the user to pick a pool — there is NO second selector here.
    expect(screen.getByText("Choose an assistant at the top left to start a conversation.")).toBeTruthy();
    expect(screen.queryByTestId("hero-pool-select")).toBeNull();
  });
  it("renders the mascot + hero composer when no session is selected", () => {
    const { container } = render(<ChatView {...baseProps} sessionId={null} />);
    expect(screen.getByRole("img", { name: "ModexBot mascot" })).toBeTruthy();
    expect(container.querySelector("h1")).toBeNull();
    expect(container.querySelector("form.hero-composer")).toBeTruthy();
    expect(container.querySelector("textarea")).toBeTruthy();
  });

  it("does not render the hero view once a session is selected", () => {
    const { container } = render(
      <ChatView {...baseProps} sessionId="test-session.main" agentName="main" />,
    );
    expect(container.querySelector("form.hero-composer")).toBeNull();
    expect(container.querySelector("form.composer")).toBeTruthy();
    // Only the header mascot remains — the hero mascot left with the hero.
    const mascots = screen.getAllByRole("img", { name: "ModexBot mascot" });
    expect(mascots).toHaveLength(1);
    expect(container.querySelector("header")!.contains(mascots[0]!)).toBe(true);
  });

  it("disables the attach button in hero mode (no session to upload to)", () => {
    const { container } = render(<ChatView {...baseProps} sessionId={null} />);
    const attachBtn = container.querySelector("button[aria-label]");
    expect(attachBtn).toBeTruthy();
  });

  it("cycles the hero hint every 5s and resets to the first hint on pool change", () => {
    vi.useFakeTimers();
    try {
      const { rerender } = render(<ChatView {...baseProps} sessionId={null} pool="main" />);
      expect(screen.getByText("What can I help you build?")).toBeTruthy();
      act(() => {
        vi.advanceTimersByTime(5000);
      });
      expect(screen.getByText("Type / to pick a skill")).toBeTruthy();
      // Pool switch remounts the keyed hero group — rotation restarts at hint 1.
      rerender(<ChatView {...baseProps} sessionId={null} pool="coder" />);
      expect(screen.getByText("What can I help you build?")).toBeTruthy();
    } finally {
      vi.useRealTimers();
    }
  });

  it("shows no pool heading or selector in the hero (pool only feeds routing)", () => {
    const { rerender } = render(<ChatView {...baseProps} sessionId={null} pool="default" />);
    expect(screen.queryByRole("heading")).toBeNull();
    expect(screen.queryByRole("combobox")).toBeNull();
    rerender(<ChatView {...baseProps} sessionId={null} pool="coder" />);
    expect(screen.getByRole("img", { name: "ModexBot mascot" })).toBeTruthy();
  });

  it("keeps the sidebar pool selector reachable from the mobile hero", () => {
    const openSidebar = vi.fn();
    render(<ChatView {...baseProps} sessionId={null} onOpenSidebar={openSidebar} />);
    fireEvent.click(screen.getByRole("button", { name: "Open sidebar" }));
    expect(openSidebar).toHaveBeenCalledOnce();
  });

  it("pulses the composer when heroFocusNonce bumps", () => {
    const { container, rerender } = render(
      <ChatView {...baseProps} sessionId={null} heroFocusNonce={0} />,
    );
    rerender(<ChatView {...baseProps} sessionId={null} heroFocusNonce={1} />);
    const form = container.querySelector("form.hero-composer");
    expect(form?.classList.contains("composer-pulse")).toBe(true);
  });

  it("announces the new conversation to screen readers on bump", async () => {
    const { rerender } = render(
      <ChatView {...baseProps} sessionId={null} heroFocusNonce={0} />,
    );
    rerender(<ChatView {...baseProps} sessionId={null} heroFocusNonce={1} />);
    await waitFor(() => {
      expect(
        screen.getByText("New conversation. Type a message to begin."),
      ).toBeTruthy();
    });
  });
});
