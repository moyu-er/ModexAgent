import { describe, it, expect, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
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
  it("renders ModexBot wordmark + hero composer when no session is selected", () => {
    const { container } = render(<ChatView {...baseProps} sessionId={null} />);
    expect(screen.getByText("ModexBot")).toBeTruthy();
    expect(screen.getByText("ModexBot").className).toContain("hero-wordmark");
    expect(container.querySelector("form.hero-composer")).toBeTruthy();
    expect(container.querySelector("textarea")).toBeTruthy();
  });

  it("does not render the hero view once a session is selected", () => {
    const { container } = render(
      <ChatView {...baseProps} sessionId="test-session.main" agentName="main" />,
    );
    expect(container.querySelector(".hero-wordmark")).toBeNull();
    expect(container.querySelector("form.hero-composer")).toBeNull();
    expect(container.querySelector("form.composer")).toBeTruthy();
  });

  it("disables the attach button in hero mode (no session to upload to)", () => {
    const { container } = render(<ChatView {...baseProps} sessionId={null} />);
    const attachBtn = container.querySelector("button[aria-label]");
    expect(attachBtn).toBeTruthy();
  });

  it("renders the new-conversation eyebrow with the target pool", () => {
    const { rerender } = render(<ChatView {...baseProps} sessionId={null} pool="main" />);
    expect(screen.getByText("New conversation · main")).toBeTruthy();
    // The eyebrow follows the sidebar's pool selection live and replays the
    // entry animation on change (keyed remount).
    rerender(<ChatView {...baseProps} sessionId={null} pool="coder" />);
    expect(screen.getByText("New conversation · coder")).toBeTruthy();
  });

  it("uses the selected pool for the animated wordmark itself", () => {
    const { rerender } = render(<ChatView {...baseProps} sessionId={null} pool="default" />);
    expect(screen.getByRole("heading", { level: 1, name: "default" }).className).toContain("hero-wordmark");
    rerender(<ChatView {...baseProps} sessionId={null} pool="coder" />);
    expect(screen.getByRole("heading", { level: 1, name: "coder" }).className).toContain("hero-wordmark");
    expect(screen.queryByRole("combobox")).toBeNull();
    rerender(<ChatView {...baseProps} sessionId={null} pool="default" />);
    expect(screen.getAllByRole("heading", { level: 1 })).toHaveLength(1);
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
