import { useState, useRef, useEffect, type FC, type FormEvent, type KeyboardEvent } from "react";
import { Bot, File, Menu, Paperclip, Pause, SendHorizonal, X } from "lucide-react";
import type { ApprovalRequestView, TodoItemDTO, UIMessage } from "../types/events";
import type { MediaConfigResponse, OutgoingAttachmentRef, UploadAttachmentResponse } from "../types/attachments";
import { ApprovalCard } from "./ApprovalCard";
import { MessageBubble } from "./MessageBubble";
import { ModelSelector } from "./ModelSelector";
import { TodoPanel } from "./TodoPanel";
import { Button } from "./ui/Button";
import { IconButton } from "./ui/IconButton";
import { fetchMediaConfig, fetchModels, uploadAttachment, type ModelChoice } from "../lib/api";
import { formatBytes } from "../lib/format";

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
  onSend: (
    content: string,
    attachments?: OutgoingAttachmentRef[],
    providerName?: string,
    modelName?: string,
  ) => void;
  /** Invoked when the user presses the pause control on a streaming session. */
  onPause?: () => void;
  readOnly?: boolean;
  onOpenSidebar?: () => void;
  /** Display name of the selected session's agent (shown in the chat header).
   * Omitted/empty when no session is open → the header label is blank. */
  agentName?: string;
}

// Input box starts as a single comfortable line and grows with content.
const MAX_INPUT_HEIGHT = 320;
const MIN_INPUT_HEIGHT = 56;

// Chat column is capped at 1200px and centered; keep a reasonable floor
// on desktop so the dialog doesn't collapse too narrowly.
const CONTENT_WIDTH = "mx-auto w-full min-w-0 max-w-[1200px] md:min-w-[720px]";

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
  agentName,
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

  // Available models for the composer selector. Loaded once on mount; the
  // default (or first) choice is preselected so a turn is always routed
  // somewhere even if the user never touches the dropdown. Failures are
  // swallowed — the selector just renders empty and the send falls back to
  // the backend's configured default.
  const [models, setModels] = useState<ModelChoice[]>([]);
  const [selected, setSelected] = useState<{ provider: string; model: string }>(
    { provider: "", model: "" },
  );

  useEffect(() => {
    fetchModels()
      .then((r) => {
        setModels(r.choices);
        const d = r.choices.find((c) => c.default) ?? r.choices[0];
        if (d) setSelected({ provider: d.provider_name, model: d.model_name });
      })
      .catch(() => {});
  }, []);

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
    onSend(
      trimmed,
      pendingUploads.map((p) => p.ref),
      selected.provider,
      selected.model,
    );
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
            `"${file.name}" is too large (${formatBytes(file.size)}). ` +
            `Limit is ${formatBytes(earlyCap)}.`,
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
    <div className="flex h-full flex-col bg-canvas">
      {/* Header */}
      <header className="flex h-14 shrink-0 items-center justify-between border-b border-hairline px-4">
        <div className="flex items-center gap-3">
          {onOpenSidebar && (
            <IconButton
              icon={<Menu size={18} />}
              label="Open sidebar"
              variant="ghost"
              size="md"
              onClick={onOpenSidebar}
              className="md:hidden"
            />
          )}
          {agentName && (
            <>
              <Bot size={15} className="text-signal" aria-hidden="true" />
              <span className="font-mono text-sm font-semibold text-ink">
                {agentName}
              </span>
            </>
          )}
        </div>
        <div />
      </header>

      {/* Message area */}
      <div className="flex-1 overflow-y-auto">
        <div className={`${CONTENT_WIDTH} px-3 py-6 md:px-5`}>
          {messages.length === 0 && (
            <div className="flex h-[55vh] items-center justify-center">
              <p className="text-sm text-body">
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
            <div className="my-2 flex items-center justify-between gap-2 rounded-md border border-hairline bg-canvas-elevated px-3 py-2">
              <span className="text-xs text-body">
                Denying any one cancels the whole batch
              </span>
              <Button
                variant="secondary"
                size="sm"
                disabled={isApprovingBatch}
                onClick={onApproveAll}
              >
                Approve All
              </Button>
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
                className="flex-1 cursor-not-allowed bg-transparent py-1 text-sm text-faint placeholder:text-faint outline-none"
              />
              <IconButton
                icon={<SendHorizonal size={18} />}
                label="Read only"
                variant="ghost"
                size="md"
                disabled
                title="Read only"
              />
            </div>
          ) : (
            <>
              {(pendingUploads.length > 0 || uploadError) && (
                <div className="mb-2 flex flex-col gap-1.5">
                  {uploadError && (
                    <div className="rounded-md border border-error/40 bg-error/10 px-2.5 py-1.5 text-xs text-error">
                      {uploadError}
                    </div>
                  )}
                  {pendingUploads.map((p) => (
                    <div
                      key={p.ref.local_path}
                      className="flex items-center gap-2 rounded-md border border-hairline bg-canvas-elevated px-2.5 py-1.5 text-xs"
                    >
                      <File size={14} className="shrink-0 text-body" aria-hidden="true" />
                      <span className="min-w-0 flex-1 truncate text-ink">
                        {p.name}
                      </span>
                      <span className="shrink-0 text-body">
                        {formatBytes(p.size)}
                      </span>
                      <IconButton
                        icon={<X size={14} />}
                        label={`Remove ${p.name}`}
                        variant="ghost"
                        size="sm"
                        onClick={(): void => removePendingUpload(p.ref.local_path)}
                      />
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
                <IconButton
                  icon={<Paperclip size={18} />}
                  label="Attach file"
                  variant="ghost"
                  size="md"
                  disabled={isBusy || isUploading || !sessionId}
                  onClick={(): void => fileInputRef.current?.click()}
                />
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
                  className="max-h-[320px] min-h-[56px] flex-1 resize-none overflow-y-auto bg-transparent py-3.5 text-[15px] leading-relaxed text-ink outline-none placeholder:text-faint"
                />
                {models.length > 0 && (
                  <ModelSelector
                    models={models}
                    value={selected}
                    onChange={setSelected}
                  />
                )}
                {isBusy ? (
                  <IconButton
                    icon={<Pause size={16} />}
                    label="Pause"
                    variant="secondary"
                    size="md"
                    onClick={handleButton}
                  />
                ) : canSend ? (
                  <IconButton
                    icon={<SendHorizonal size={18} />}
                    label="Send"
                    variant="primary"
                    size="md"
                    onClick={handleButton}
                  />
                ) : (
                  <IconButton
                    icon={<SendHorizonal size={18} />}
                    label="Send"
                    variant="ghost"
                    size="md"
                    disabled
                    onClick={handleButton}
                  />
                )}
              </form>
            </>
          )}
        </div>
      </div>
    </div>
  );
};