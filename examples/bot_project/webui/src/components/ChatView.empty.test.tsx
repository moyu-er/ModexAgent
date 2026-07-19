import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
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

describe("ChatView empty state (Teal & Ember §6)", () => {
  it("shows logo watermark + display headline + eyebrow hints when no conversation is selected", () => {
    const { container } = render(<ChatView {...baseProps} />);
    // Display-font headline replaces the bare one-liner.
    const headline = screen.getByText("Chat with your agents");
    expect(headline.className).toContain("font-display");
    // Mono-eyebrow hints (2–3 lines).
    expect(screen.getByText(/select a conversation/i)).toBeTruthy();
    expect(screen.getByText(/new conversation/i)).toBeTruthy();
    // Logo watermark (brand, low opacity).
    const watermark = container.querySelector("svg[data-logo-mark]");
    expect(watermark).toBeTruthy();
  });

  it("hides the empty state once messages exist", () => {
    const messages: UIMessage[] = [
      {
        id: "m1",
        role: "user",
        agent_name: "main",
        blocks: [{ kind: "text", text: "hi" }],
        isStreaming: false,
      },
    ];
    render(<ChatView {...baseProps} messages={messages} />);
    expect(screen.queryByText("Chat with your agents")).toBeNull();
  });
});
