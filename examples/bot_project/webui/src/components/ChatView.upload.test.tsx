import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { ChatView } from "./ChatView";

// Mock the attachment API surface used by the composer's upload path.
const fetchMediaConfigMock = vi.fn();
const uploadAttachmentMock = vi.fn();
vi.mock("../lib/api", () => ({
  fetchMediaConfig: (...args: unknown[]) => fetchMediaConfigMock(...args),
  uploadAttachment: (...args: unknown[]) => uploadAttachmentMock(...args),
  fetchModels: vi.fn().mockResolvedValue({ choices: [] }),
  attachmentDownloadUrl: (sid: string, id: string, ws?: string) =>
    `/api/sessions/${sid}/attachments/${id}${ws ? `?ws=${ws}` : ""}`,
}));

const noop = vi.fn();

const defaultProps = {
  messages: [],
  isStreaming: false,
  isPending: false,
  todos: [],
  pendingApprovals: [],
  isApprovingBatch: false,
  submitApproval: noop,
  onApproveAll: noop,
  sessionId: "web:test.main",
  workspace: "",
  onSend: noop,
  readOnly: false,
};

describe("ChatView attachment upload pre-validation", () => {
  beforeEach(() => {
    fetchMediaConfigMock.mockReset();
    uploadAttachmentMock.mockReset();
  });

  it("blocks an over-size file and shows a clear notice without uploading", async () => {
    fetchMediaConfigMock.mockResolvedValue({
      max_image_bytes: 1024,
      max_text_doc_bytes: 1024,
      session_budget_bytes: 1_000_000,
      max_outbound_bytes: 1_000_000,
    });

    render(<ChatView {...defaultProps} />);

    const input = document.querySelector(
      'input[type="file"]',
    ) as HTMLInputElement;
    expect(input).toBeTruthy();

    const huge = new File([new Uint8Array(2048)], "big.png", { type: "image/png" });
    fireEvent.change(input, { target: { files: [huge] } });

    await waitFor(() => {
      expect(screen.getByText(/too large/i)).toBeTruthy();
    });

    // The over-size file must NOT have been uploaded.
    expect(uploadAttachmentMock).not.toHaveBeenCalled();
    // No pending-upload chip for the rejected file.
    expect(screen.queryByText("big.png")).toBeNull();
  });

  it("uploads an accepted file and includes its ref in onSend", async () => {
    fetchMediaConfigMock.mockResolvedValue({
      max_image_bytes: 1_000_000,
      max_text_doc_bytes: 1_000_000,
      session_budget_bytes: 10_000_000,
      max_outbound_bytes: 10_000_000,
    });
    uploadAttachmentMock.mockResolvedValue({
      local_path: "/tmp/uploads/abc.png",
      filename: "pic.png",
      size: 512,
      mime: "image/png",
    });
    const onSend = vi.fn();

    render(<ChatView {...defaultProps} onSend={onSend} />);

    const input = document.querySelector(
      'input[type="file"]',
    ) as HTMLInputElement;
    const file = new File([new Uint8Array(512)], "pic.png", { type: "image/png" });
    fireEvent.change(input, { target: { files: [file] } });

    // The chip appears once the upload resolves.
    await waitFor(() => {
      expect(screen.getByText("pic.png")).toBeTruthy();
    });
    expect(uploadAttachmentMock).toHaveBeenCalledTimes(1);

    // Submit with empty text — the pending upload alone should fire onSend
    // carrying the attachment ref.
    const sendBtn = screen.getByRole("button", { name: /Send/i });
    fireEvent.click(sendBtn);

    expect(onSend).toHaveBeenCalledTimes(1);
    const [, attachments] = onSend.mock.calls[0]!;
    expect(attachments).toEqual([
      {
        local_path: "/tmp/uploads/abc.png",
        filename: "pic.png",
        mime: "image/png",
      },
    ]);
  });

  it("hero mode: file is held client-side (no upload) and passed to onHeroSend as File", async () => {
    fetchMediaConfigMock.mockResolvedValue({
      max_image_bytes: 1_000_000,
      max_text_doc_bytes: 1_000_000,
      session_budget_bytes: 10_000_000,
      max_outbound_bytes: 10_000_000,
    });
    const onHeroSend = vi.fn();

    render(<ChatView {...defaultProps} sessionId={null} onHeroSend={onHeroSend} />);

    const input = document.querySelector(
      'input[type="file"]',
    ) as HTMLInputElement;
    expect(input).toBeTruthy();

    const file = new File([new Uint8Array(512)], "pic.png", { type: "image/png" });
    fireEvent.change(input, { target: { files: [file] } });

    await waitFor(() => {
      expect(screen.getByText("pic.png")).toBeTruthy();
    });

    expect(uploadAttachmentMock).not.toHaveBeenCalled();

    const sendBtn = screen.getByRole("button", { name: /Send/i });
    fireEvent.click(sendBtn);

    expect(onHeroSend).toHaveBeenCalledTimes(1);
    const [, files] = onHeroSend.mock.calls[0]!;
    expect(files).toEqual([file]);
  });
});
