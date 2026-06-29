import { useState, useRef, useEffect, type FC, type FormEvent, type KeyboardEvent } from "react";
import type { ApprovalRequestView, TodoItemDTO, UIMessage } from "../types/events";
import type { MediaConfigResponse, OutgoingAttachmentRef, UploadAttachmentResponse } from "../types/attachments";
import { ApprovalCard } from "./ApprovalCard";
import { MessageBubble } from "./MessageBubble";
import { TodoPanel } from "./TodoPanel";
import { fetchMediaConfig, uploadAttachment } from "../lib/api";

export interface ChatViewProps {
  messages: UIMessage[];
  isStreaming: boolean;
  isPending: boolean;
  /** Active todos (pending + in_progress) for the selected session. */
  todos: TodoItemDTO[];
  /** Pending approvals for the selected session (awaiting user decision). */
  pendingApprovals: ApprovalRequestView[];
  /** True while any approval POST is in flight — disables all approval buttons. */
  isApprovingBatch: boolean;
  /** POST an allow/deny decision for a pending approval. */
  submitApproval: (toolCallId: string, action: "allow" | "deny") => void;
  /** Approve every currently-pending card at once. */
  onApproveAll: () => void;
  /** Current session id — passed to TodoPanel so it auto-closes on switch. */
  sessionId?: string | null;
  /** Active workspace (empty/undefined = home) — for attachment download URLs. */
  workspace?: string;
  onSend: (content: string, attachments?: OutgoingAttachmentRef[]) => void;
  /** Invoked when the user presses the pause control on a streaming session. */
  onPause?: () => void;
  readOnly?: boolean;
  onOpenSidebar?: () => void;
}

// Input box starts as a single comfortable line and grows with content.
const MAX_INPUT_HEIGHT = 320;
const MIN_INPUT_HEIGHT = 56;

// Chat column is capped at 1200px and centered; keep a reasonable floor
// on desktop so the dialog doesn't collapse too narrowly.
const CONTENT_WIDTH = "mx-auto w-full min-w-0 max-w-[1200px] md:min-w-[720px]";

/** Format a byte count for inline upload notices (1.2 KB, 3.4 MB). */
function formatLocalBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes < 0) return "—";
  if (bytes < 1024) return `${bytes} B`;
  const units = ["KB", "MB", "GB"];
  let value = bytes / 1024;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value.toFixed(1)} ${units[unit]}`;
}

export const ChatView: FC<ChatViewProps> = ({
  messages,
  isStreaming,
  isPending,
  todos,
  pendingApprovals,
  isApprovingBatch,
  submitApproval,
  onApproveAll,
  sessionId,
  workspace,
  onSend,
  onPause,
  readOnly = false,
  onOpenSidebar,
}) => {
  const [input, setInput] = useState("");
  const bottomRef = useRef<HTMLDivElement | null>(null);
  const taRef = useRef<HTMLTextAreaElement | null>(null);
  const fileInputRef = useRef<HTMLInputElement | null>(null);

  // Pending uploads collected in the composer before the message is sent.
  // Each entry is a successfully uploaded file (a backend ref the WS
  // send_message will carry as an attachment). ``error`` surfaces the most
  // recent pre-validation / upload failure to the user as a notice.
  interface PendingUpload {
    ref: OutgoingAttachmentRef;
    name: string;
    size: number;
  }
  const [pendingUploads, setPendingUploads] = useState<PendingUpload[]>([]);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const [isUploading, setIsUploading] = useState<boolean>(false);

  // MediaConfig is fetched once on first use (cached for the session). The
  // authoritative per-kind gate runs in the ingest stage; this is only the
  // loose client-side pre-check (ADR-0013 §7).
  const mediaConfigRef = useRef<MediaConfigResponse | null>(null);
  const fetchMediaConfigCached = async (): Promise<MediaConfigResponse> => {
    if (!mediaConfigRef.current) {
      mediaConfigRef.current = await fetchMediaConfig();
    }
    return mediaConfigRef.current;
  };

  // Auto-scroll to bottom when messages change
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  // Grow the textarea with its content up to a capped height; once the cap is
  // reached the textarea scrolls internally instead of growing the composer.
  const autosize = (): void => {
    const ta = taRef.current;
    if (!ta) return;
    ta.style.height = "auto";
    ta.style.height = `${Math.max(
      MIN_INPUT_HEIGHT,
      Math.min(ta.scrollHeight, MAX_INPUT_HEIGHT),
    )}px`;
  };
  useEffect(() => {
    autosize();
  }, [input]);

  const isBusy = isStreaming || isPending;
  const canSend =
    !isBusy && !readOnly && !isUploading &&
    (input.trim().length > 0 || pendingUploads.length > 0);

  const submit = (): void => {
    if (readOnly || isBusy) return;
    const trimmed = input.trim();
    if (!trimmed && pendingUploads.length === 0) return;
    onSend(trimmed, pendingUploads.map((p) => p.ref));
    setInput("");
    setPendingUploads([]);
    setUploadError(null);
  };

  const handleSubmit = (e: FormEvent<HTMLFormElement>): void => {
    e.preventDefault();
    submit();
  };

  const handleKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>): void => {
    // Ignore Enter while an IME is composing (e.g. Chinese / Japanese / Korean
    // input on macOS).  Pressing Enter to confirm a composition candidate
    // fires a keydown with ``e.key === "Enter"`` — without this guard the
    // message is sent prematurely.  ``isComposing`` is the standard flag;
    // ``keyCode === 229`` is the legacy fallback for older Safari/Edge.
    if (e.nativeEvent.isComposing || e.keyCode === 229) return;
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      submit();
    }
  };

  const handleButton = (): void => {
    if (isBusy) {
      onPause?.();
      return;
    }
    submit();
  };

  // File selection → loose client-side pre-validation (size against the
  // fetched MediaConfig's generous cap = max of image/text-doc limits) → POST
  // to the upload endpoint → collect the returned ref as a pending upload chip.
  // The authoritative per-kind gate runs later in the ingest stage. ``ws``
  // scopes the temp file to the active workspace; home (empty) omits it.
  const handleFileSelect = async (e: React.ChangeEvent<HTMLInputElement>): Promise<void> => {
    if (!sessionId) {
      setUploadError("Select a conversation before attaching a file.");
      return;
    }
    const files = e.target.files;
    if (!files || files.length === 0) return;
    setUploadError(null);
    setIsUploading(true);
    try {
      const config = await fetchMediaConfigCached();
      const earlyCap = Math.max(config.max_image_bytes, config.max_text_doc_bytes);
      const accepted: PendingUpload[] = [];
      for (const file of Array.from(files)) {
        if (file.size > earlyCap) {
          setUploadError(
            `"${file.name}" is too large (${formatLocalBytes(file.size)}). ` +
            `Limit is ${formatLocalBytes(earlyCap)}.`,
          );
          continue;
        }
        const uploaded: UploadAttachmentResponse = await uploadAttachment(
          sessionId,
          file,
          workspace || undefined,
        );
        accepted.push({
          ref: {
            local_path: uploaded.local_path,
            filename: uploaded.filename,
            mime: uploaded.mime ?? undefined,
          },
          name: uploaded.filename,
          size: uploaded.size,
        });
      }
      if (accepted.length > 0) {
        setPendingUploads((prev) => [...prev, ...accepted]);
      }
    } catch (err) {
      setUploadError(err instanceof Error ? err.message : "Upload failed.");
    } finally {
      setIsUploading(false);
      // Reset so selecting the same file again fires another change event.
      if (fileInputRef.current) fileInputRef.current.value = "";
    }
  };

  const removePendingUpload = (localPath: string): void => {
    setPendingUploads((prev) => prev.filter((p) => p.ref.local_path !== localPath));
  };

  return (
    <div className="flex h-full flex-col bg-page-bg-light dark:bg-page-bg-dark">
      {/* Header */}
      <header className="flex h-14 shrink-0 items-center justify-between border-b border-divider-light dark:border-divider-dark px-4">
        <div className="flex items-center gap-3">
          {onOpenSidebar && (
            <button
              type="button"
              onClick={onOpenSidebar}
              className="rounded-md p-2 text-text-secondary-light dark:text-text-secondary-dark transition-colors hover:bg-sidebar-hover-light dark:hover:bg-sidebar-hover-dark hover:text-text-primary-light dark:hover:text-text-primary-dark md:hidden"
              aria-label="Open sidebar"
            >
              <MenuIcon />
            </button>
          )}
          <span className="text-sm font-semibold text-text-primary-light dark:text-text-primary-dark">
            ModexBot
          </span>
        </div>
        <div />
      </header>

      {/* Message area */}
      <div className="flex-1 overflow-y-auto">
        <div className={`${CONTENT_WIDTH} px-3 py-6 md:px-5`}>
          {messages.length === 0 && (
            <div className="flex h-[55vh] items-center justify-center">
              <p className="text-sm text-text-secondary-light dark:text-text-secondary-dark">
                Select a conversation to start chatting
              </p>
            </div>
          )}
          {messages.map((msg) => (
            <MessageBubble
              key={msg.id}
              message={msg}
              sessionId={sessionId}
              workspace={workspace}
            />
          ))}
          {pendingApprovals.length > 0 && (
            <div className="my-2 flex items-center justify-between gap-2 rounded-lg border border-card-border-light bg-content-bg-light px-3 py-2 dark:border-card-border-dark dark:bg-content-bg-dark">
              <span className="text-xs text-text-secondary-light dark:text-text-secondary-dark">
                Denying any one cancels the whole batch
              </span>
              <button
                type="button"
                disabled={isApprovingBatch}
                onClick={onApproveAll}
                className="rounded border border-approve-light/50 px-3 py-1 text-sm font-medium text-approve-light transition-colors hover:bg-approve-light/10 disabled:cursor-not-allowed disabled:opacity-50 dark:border-approve-dark/50 dark:text-approve-dark dark:hover:bg-approve-dark/10"
              >
                Approve All
              </button>
            </div>
          )}
          {pendingApprovals.map((view) => (
            <ApprovalCard
              key={view.tool_call_id}
              view={view}
              disabled={isApprovingBatch}
              onApprove={(id) => submitApproval(id, "allow")}
              onDeny={(id) => submitApproval(id, "deny")}
            />
          ))}
          <div ref={bottomRef} />
        </div>
      </div>

      {/* Floating todo widget — outside the scroll area so it stays visible */}
      <TodoPanel todos={todos} sessionId={sessionId} />

      {/* Floating composer */}
      <div className="px-3 pb-6 pt-2 md:px-5">
        <div className={CONTENT_WIDTH}>
          {readOnly ? (
            <div className="composer">
              <input
                type="text"
                disabled
                placeholder="Subagent session — read only"
                className="flex-1 cursor-not-allowed bg-transparent py-1 text-sm text-text-disabled-light dark:text-text-disabled-dark placeholder-input-placeholder-light dark:placeholder-input-placeholder-dark outline-none"
              />
              <button type="button" disabled title="Read only" className="send-btn send-btn--disabled">
                <SendIcon />
              </button>
            </div>
          ) : (
            <>
              {(pendingUploads.length > 0 || uploadError) && (
                <div className="mb-2 flex flex-col gap-1.5">
                  {uploadError && (
                    <div className="rounded-md border border-deny-light/40 bg-deny-light/10 px-2.5 py-1.5 text-xs text-deny-light dark:border-deny-dark/40 dark:bg-deny-dark/10 dark:text-deny-dark">
                      {uploadError}
                    </div>
                  )}
                  {pendingUploads.map((p) => (
                    <div
                      key={p.ref.local_path}
                      className="flex items-center gap-2 rounded-md border border-card-border-light bg-content-bg-light px-2.5 py-1.5 text-xs dark:border-card-border-dark dark:bg-content-bg-dark"
                    >
                      <FileChipIcon />
                      <span className="min-w-0 flex-1 truncate text-text-primary-light dark:text-text-primary-dark">
                        {p.name}
                      </span>
                      <span className="shrink-0 text-text-secondary-light dark:text-text-secondary-dark">
                        {formatLocalBytes(p.size)}
                      </span>
                      <button
                        type="button"
                        onClick={(): void => removePendingUpload(p.ref.local_path)}
                        aria-label={`Remove ${p.name}`}
                        className="shrink-0 rounded p-0.5 text-text-secondary-light transition-colors hover:bg-sidebar-hover-light hover:text-text-primary-light dark:text-text-secondary-dark dark:hover:bg-sidebar-hover-dark dark:hover:text-text-primary-dark"
                      >
                        <RemoveIcon />
                      </button>
                    </div>
                  ))}
                </div>
              )}
              <form onSubmit={handleSubmit} className="composer">
                <input
                  ref={fileInputRef}
                  type="file"
                  multiple
                  onChange={handleFileSelect}
                  className="hidden"
                  aria-hidden="true"
                  tabIndex={-1}
                />
                <button
                  type="button"
                  onClick={(): void => fileInputRef.current?.click()}
                  disabled={isBusy || isUploading || !sessionId}
                  title="Attach file"
                  aria-label="Attach file"
                  className="composer-icon-btn"
                >
                  <PaperclipIcon />
                </button>
                <textarea
                  ref={taRef}
                  value={input}
                  onChange={(e): void => setInput(e.target.value)}
                  onInput={autosize}
                  onKeyDown={handleKeyDown}
                  placeholder={
                    isPending
                      ? "Initializing session…"
                      : isStreaming
                        ? "Assistant is responding…"
                        : "Message…"
                  }
                  rows={1}
                  className="max-h-[320px] min-h-[56px] flex-1 resize-none overflow-y-auto bg-transparent py-3.5 text-[15px] leading-relaxed text-text-primary-light dark:text-text-primary-dark outline-none placeholder-input-placeholder-light dark:placeholder-input-placeholder-dark"
                />
                <button
                  type="button"
                  onClick={handleButton}
                  title={isBusy ? "Pause" : "Send"}
                  aria-label={isBusy ? "Pause" : "Send"}
                  className={
                    isBusy
                      ? "send-btn send-btn--busy"
                      : canSend
                        ? "send-btn send-btn--active"
                        : "send-btn send-btn--disabled"
                  }
                >
                  {isBusy ? <PauseIcon /> : <SendIcon />}
                </button>
              </form>
            </>
          )}
        </div>
      </div>
    </div>
  );
};

const MenuIcon: FC = () => (
  <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    <line x1="4" y1="6" x2="20" y2="6" />
    <line x1="4" y1="12" x2="20" y2="12" />
    <line x1="4" y1="18" x2="20" y2="18" />
  </svg>
);

const SendIcon = (): JSX.Element => (
  <svg
    width="18"
    height="18"
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    strokeWidth="2"
    strokeLinecap="round"
    strokeLinejoin="round"
    aria-hidden="true"
  >
    <path d="M14.536 21.686a.5.5 0 0 0 .937-.024l6.5-19a.496.496 0 0 0-.635-.635l-19 6.5a.5.5 0 0 0-.024.937l7.93 3.18a2 2 0 0 1 1.112 1.11z" />
    <path d="m21.854 2.147-10.94 10.939" />
  </svg>
);

const PauseIcon = (): JSX.Element => (
  <svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
    <rect x="6" y="5" width="4" height="14" rx="1.2" />
    <rect x="14" y="5" width="4" height="14" rx="1.2" />
  </svg>
);

const PaperclipIcon: FC = () => (
  <svg
    width="18"
    height="18"
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    strokeWidth="2"
    strokeLinecap="round"
    strokeLinejoin="round"
    aria-hidden="true"
  >
    <path d="m21.44 11.05-9.19 9.19a6 6 0 0 1-8.49-8.49l8.57-8.57A4 4 0 1 1 17.93 8.8l-8.59 8.57a2 2 0 0 1-2.83-2.83l8.49-8.48" />
  </svg>
);

const FileChipIcon: FC = () => (
  <svg
    width="14"
    height="14"
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    strokeWidth="2"
    strokeLinecap="round"
    strokeLinejoin="round"
    aria-hidden="true"
    className="shrink-0 text-text-secondary-light dark:text-text-secondary-dark"
  >
    <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
    <polyline points="14 2 14 8 20 8" />
  </svg>
);

const RemoveIcon: FC = () => (
  <svg
    width="14"
    height="14"
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    strokeWidth="2"
    strokeLinecap="round"
    strokeLinejoin="round"
    aria-hidden="true"
  >
    <line x1="18" y1="6" x2="6" y2="18" />
    <line x1="6" y1="6" x2="18" y2="18" />
  </svg>
);
