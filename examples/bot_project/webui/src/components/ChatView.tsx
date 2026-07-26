import { useState, useRef, useEffect, useMemo, type FC, type FormEvent, type KeyboardEvent, type ReactNode } from "react";
import { Bot, File, Menu, Paperclip, Pause, SendHorizonal, X } from "lucide-react";
import type { ApprovalRequestView, TodoItemDTO, UIMessage } from "../types/events";
import type { MediaConfigResponse, OutgoingAttachmentRef, UploadAttachmentResponse } from "../types/attachments";
import { ApprovalCard } from "./ApprovalCard";
import { ConversationSpine, type SpineAnchor } from "./ConversationSpine";
import { MessageBubble } from "./MessageBubble";
import { ModelSelector } from "./ModelSelector";
import { CommandSuggest, activeQuery, filterSuggestions, buildInsertion, handleSuggestKey } from "./CommandSuggest";
import { TodoPanel } from "./TodoPanel";
import { Button } from "./ui/Button";
import { IconButton } from "./ui/IconButton";
import { fetchMediaConfig, fetchModels, uploadAttachment, type ModelChoice } from "../lib/api";
import { useCommandSuggestions } from "../hooks/useCommandSuggestions";
import { formatBytes } from "../lib/format";
import { useT } from "../i18n";

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
  /** Send handler used when no session is selected (hero composer). The App
   *  creates a client-side draft and calls send() immediately — send() adds
   *  the optimistic message and queues the ws send for the `attached`
   *  handler to flush with the real session id. */
  onHeroSend?: (
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
  /** Pool of the selected session (existing session) or the active pool (hero).
   *  Used to resolve the skill set for /skillName autocomplete. */
  pool?: string;
  /** Monotonic counter from useSessions.handleNew. Each bump means the user
   *  clicked "New Conversation": focus the hero composer and replay the
   *  acknowledgment pulse (the click is otherwise invisible when the hero
   *  view is already showing). */
  heroFocusNonce?: number;
}

// Input box starts as a single comfortable line and grows with content.
const MAX_INPUT_HEIGHT = 320;
const MIN_INPUT_HEIGHT = 56;
const MIN_HERO_INPUT_HEIGHT = 96;

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
  onHeroSend,
  onPause,
  readOnly = false,
  onOpenSidebar,
  agentName,
  pool,
  heroFocusNonce = 0,
}) => {
  const t = useT();
  const [input, setInput] = useState("");
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const contentRef = useRef<HTMLDivElement | null>(null);
  const taRef = useRef<HTMLTextAreaElement | null>(null);
  const formRef = useRef<HTMLFormElement | null>(null);
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const [announcement, setAnnouncement] = useState("");

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

  // Fetch models on mount. If the backend returns an empty list (config not
  // loaded yet — common right after a restart), retry a few times with backoff
  // so the selector populates once model.yml is available.
  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | null = null;
    const delays = [0, 2000, 5000, 10000, 15000];

    const attempt = (i: number): void => {
      if (cancelled || i >= delays.length) return;
      timer = setTimeout(() => {
        if (cancelled) return;
        fetchModels()
          .then((r) => {
            if (cancelled) return;
            if (r.choices.length > 0) {
              setModels(r.choices);
              const d = r.choices.find((c) => c.default) ?? r.choices[0];
              if (d) setSelected({ provider: d.provider_name, model: d.model_name });
            } else {
              attempt(i + 1);
            }
          })
          .catch(() => attempt(i + 1));
      }, delays[i]);
    };

    attempt(0);
    return (): void => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, []);

  // Slash-command autocomplete: skills + built-in commands, merged by
  // useCommandSuggestions. Warmup fills the skill cache; the read hook
  // returns [] until the cache is populated.
  const suggestions = useCommandSuggestions(pool, agentName);
  const [caret, setCaret] = useState(0);
  const [suggestActive, setSuggestActive] = useState(0);
  const suggestQuery = activeQuery(input, caret);
  const suggestMatches = useMemo(
    () => (suggestQuery === null ? [] : filterSuggestions(suggestions, suggestQuery)),
    [suggestions, suggestQuery],
  );
  const suggestOpen = suggestQuery !== null && suggestMatches.length > 0;
  useEffect(() => {
    setSuggestActive(0);
  }, [suggestMatches]);
  const applySuggestInsert = (newInput: string, newCaret: number): void => {
    setInput(newInput);
    setCaret(newCaret);
    requestAnimationFrame(() => {
      const ta = taRef.current;
      if (ta) {
        ta.focus();
        ta.setSelectionRange(newCaret, newCaret);
      }
    });
  };
  const chooseSuggestion = (idx: number): void => {
    const item = suggestMatches[idx];
    if (!item) return;
    const { input: newInput, caret: newCaret } = buildInsertion(input, caret, item.name);
    applySuggestInsert(newInput, newCaret);
  };

  // Auto-scroll the message list to the bottom when messages change. We scroll
  // the scroll container directly (setting scrollTop) rather than calling
  // scrollIntoView on a sentinel: scrollIntoView defaults to block:"start",
  // which pins the bottom sentinel to the *top* of the viewport (leaving the
  // last message scrolled up and blank space below), and it also moves every
  // scrollable ancestor — both cause the "page jumps, blank below" symptom.
  useEffect(() => {
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [messages]);

  const isHero = sessionId == null;
  const minInputHeight = isHero ? MIN_HERO_INPUT_HEIGHT : MIN_INPUT_HEIGHT;

  // Grow the textarea with its content up to a capped height; once the cap is
  // reached the textarea scrolls internally instead of growing the composer.
  const autosize = (): void => {
    const ta = taRef.current;
    if (!ta) return;
    ta.style.height = "auto";
    ta.style.height = `${Math.max(
      minInputHeight,
      Math.min(ta.scrollHeight, MAX_INPUT_HEIGHT),
    )}px`;
  };
  useEffect(() => {
    autosize();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [input, isHero]);

  // "New Conversation" acknowledgment: focus the composer and replay the
  // brand pulse so the click always produces visible feedback, even when the
  // hero view was already on screen. Auto-focus is desktop-only — on touch
  // devices it would pop the keyboard unprompted.
  useEffect(() => {
    if (!heroFocusNonce) return;
    if (window.matchMedia("(pointer: fine)").matches) {
      taRef.current?.focus();
    }
    let pulseTimer: number | undefined;
    const form = formRef.current;
    if (form) {
      form.classList.remove("composer-pulse");
      // Forced reflow — without it, re-adding the class mid-animation is a
      // no-op and rapid repeated clicks never restart the pulse.
      void form.offsetWidth;
      form.classList.add("composer-pulse");
      const remove = (): void => form.classList.remove("composer-pulse");
      form.addEventListener("animationend", remove, { once: true });
      // Fallback: environments that never fire animationend (e.g. happy-dom).
      pulseTimer = window.setTimeout(remove, 800);
    }
    // Clear then re-set on the next tick so repeated clicks re-announce to
    // screen readers (identical text in an aria-live region is not re-read).
    setAnnouncement("");
    const announceTimer = window.setTimeout(() => {
      setAnnouncement(t("chat.newConversationAnnounce"));
    }, 50);
    return (): void => {
      if (pulseTimer !== undefined) window.clearTimeout(pulseTimer);
      window.clearTimeout(announceTimer);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [heroFocusNonce]);

  // Right-margin navigation spine: one dot per user question, positioned
  // proportionally to the question's place in the scrollable content.
  const userAnchors = useMemo<SpineAnchor[]>(() => {
    const out: SpineAnchor[] = [];
    for (const m of messages) {
      if (m.role !== "user") continue;
      const text = (m.blocks ?? [])
        .map((b) => (b.kind === "text" ? b.text : ""))
        .join("")
        .replace(/\s+/g, " ")
        .trim();
      out.push({ id: m.id, preview: text ? text.slice(0, 60) : "(message)" });
    }
    return out;
  }, [messages]);

  const isBusy = isStreaming || isPending;
  const canSend =
    !isBusy && !readOnly && !isUploading &&
    (input.trim().length > 0 || pendingUploads.length > 0);

  const submit = (): void => {
    if (readOnly || isBusy) return;
    const trimmed = input.trim();
    if (!trimmed && pendingUploads.length === 0) return;
    const send = isHero ? onHeroSend : onSend;
    if (!send) return;
    send(
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
    // Skill autocomplete: when the menu is open, arrows/Enter/Tab/Escape
    // navigate it instead of their normal composer behavior. Checked before
    // the IME guard so Escape always closes the menu even mid-composition.
    if (
      handleSuggestKey(
        e.key,
        { open: suggestOpen, count: suggestMatches.length, active: suggestActive },
        {
          setActive: setSuggestActive,
          choose: chooseSuggestion,
          close: (): void => {},
        },
      )
    ) {
      e.preventDefault();
      return;
    }
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

  // File selection — loose client-side pre-validation (size against the
  // fetched MediaConfig's generous cap = max of image/text-doc limits) → POST
  // to the upload endpoint → collect the returned ref as a pending upload chip.
  // The authoritative per-kind gate runs later in the ingest stage. ``ws``
  // scopes the temp file to the active workspace; home (empty) omits it.
  const handleFileSelect = async (e: React.ChangeEvent<HTMLInputElement>): Promise<void> => {
    if (!sessionId) {
      setUploadError(t("chat.selectConversationFirst"));
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
            t("chat.fileTooLarge", { name: file.name, size: formatBytes(file.size), limit: formatBytes(earlyCap) }),
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
      setUploadError(err instanceof Error ? err.message : t("chat.uploadFailed"));
    } finally {
      setIsUploading(false);
      // Reset so selecting the same file again fires another change event.
      if (fileInputRef.current) fileInputRef.current.value = "";
    }
  };

  const removePendingUpload = (localPath: string): void => {
    setPendingUploads((prev) => prev.filter((p) => p.ref.local_path !== localPath));
  };

  const renderComposer = (hero: boolean): ReactNode => {
    const formClassName = hero ? "hero-composer w-full" : "composer";
    const textareaClassName = hero
      ? "max-h-[320px] min-h-[96px] w-full resize-none overflow-y-auto bg-transparent py-5 text-md leading-relaxed text-ink outline-none placeholder:text-faint"
      : "max-h-[320px] min-h-[56px] w-full resize-none overflow-y-auto bg-transparent py-3.5 text-md leading-relaxed text-ink outline-none placeholder:text-faint";
    const placeholder = hero
      ? t("chat.messagePlaceholder")
      : isPending
        ? t("chat.initializingSession")
        : isStreaming
          ? t("chat.assistantResponding")
          : t("chat.messagePlaceholder");
    const attachDisabled = hero || isBusy || isUploading || !sessionId;
    return (
      <>
        {(pendingUploads.length > 0 || uploadError) && (
          <div className="mb-2 flex flex-col gap-1.5">
            {uploadError && (
              <div className="rounded-md border border-danger bg-canvas-elevated px-2.5 py-1.5 text-xs text-danger">
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
                  label={t("chat.removeName", { name: p.name })}
                  variant="ghost"
                  size="sm"
                  onClick={(): void => removePendingUpload(p.ref.local_path)}
                />
              </div>
            ))}
          </div>
        )}
        <form ref={formRef} onSubmit={handleSubmit} className={formClassName}>
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
            label={t("chat.attachFile")}
            variant="ghost"
            size="md"
            disabled={attachDisabled}
            onClick={(): void => fileInputRef.current?.click()}
          />
          <div className="relative min-w-0 flex-1">
            <textarea
              ref={taRef}
              value={input}
              onChange={(e): void => {
                setInput(e.target.value);
                setCaret(e.target.selectionStart);
              }}
              onKeyUp={(e): void => setCaret(e.currentTarget.selectionStart)}
              onClick={(e): void => setCaret(e.currentTarget.selectionStart)}
              onInput={autosize}
              onKeyDown={handleKeyDown}
              placeholder={placeholder}
              rows={1}
              className={textareaClassName}
            />
            {suggestOpen && (
              <CommandSuggest
                matches={suggestMatches}
                active={suggestActive}
                onActiveChange={setSuggestActive}
                onChoose={chooseSuggestion}
                direction={isHero ? "down" : "up"}
              />
            )}
          </div>
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
              label={t("chat.pause")}
              variant="secondary"
              size="md"
              onClick={handleButton}
            />
          ) : canSend ? (
            <IconButton
              icon={<SendHorizonal size={18} />}
              label={t("chat.send")}
              variant="primary"
              size="md"
              onClick={handleButton}
            />
          ) : (
            <IconButton
              icon={<SendHorizonal size={18} />}
              label={t("chat.send")}
              variant="ghost"
              size="md"
              disabled
              onClick={handleButton}
            />
          )}
        </form>
      </>
    );
  };

  if (isHero) {
    return (
      <div
        key="hero"
        className="hero-view-enter flex h-full flex-col items-center justify-center gap-8 bg-canvas px-4"
      >
        <div className="flex flex-col items-center gap-3">
          <h1 className="hero-wordmark">ModexBot</h1>
          <p className="font-mono text-[11px] font-semibold uppercase tracking-eyebrow text-faint">
            {pool
              ? t("chat.newConversationEyebrowPool", { pool })
              : t("chat.newConversationEyebrow")}
          </p>
        </div>
        <div className="w-full max-w-[720px]">
          {renderComposer(true)}
        </div>
        <span aria-live="polite" className="sr-only">
          {announcement}
        </span>
      </div>
    );
  }

  return (
    <div key="chat" className="hero-view-enter flex h-full flex-col bg-canvas">
      {/* Header */}
      <header className="flex h-14 shrink-0 items-center justify-between border-b border-hairline px-4">
        <div className="flex items-center gap-3">
          {onOpenSidebar && (
            <IconButton
              icon={<Menu size={18} />}
              label={t("chat.openSidebar")}
              variant="ghost"
              size="md"
              onClick={onOpenSidebar}
              className="md:hidden"
            />
          )}
          {agentName && (
            <>
              <Bot size={15} className="text-signal" aria-hidden="true" />
              <span className="font-mono text-base font-semibold text-ink">
                {agentName}
              </span>
            </>
          )}
        </div>
        <div />
      </header>

      {/* Message area — wrapped so the ConversationSpine can overlay the right
          margin without scrolling with the content. */}
      <div className="relative flex-1 min-h-0">
        <div ref={scrollRef} className="absolute inset-0 overflow-y-auto">
          <div ref={contentRef} className={`${CONTENT_WIDTH} px-3 py-6 md:px-5`}>
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
                  {t("chat.denyBatchNotice")}
                </span>
                <Button
                  variant="secondary"
                  size="sm"
                  disabled={isApprovingBatch}
                  onClick={onApproveAll}
                >
                  {t("chat.approveAll")}
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
          </div>
        </div>
        <ConversationSpine
          scrollRef={scrollRef}
          contentRef={contentRef}
          anchors={userAnchors}
        />
      </div>

      {/* Floating todo widget — outside the scroll area so it stays visible */}
      <TodoPanel todos={todos} sessionId={sessionId} />

      {/* Floating composer — pb-6 + safe-area-inset-bottom so it clears the
          mobile home indicator (§8). md: resets horizontal padding to 5. */}
      <div
        className="px-3 pb-6 pt-2 md:px-5"
        style={{ paddingBottom: "max(env(safe-area-inset-bottom, 0px), 1.5rem)" }}
      >
        <div className={CONTENT_WIDTH}>
          {readOnly ? (
            <div className="composer">
              <input
                type="text"
                disabled
                placeholder={t("chat.readOnlyPlaceholder")}
                className="flex-1 cursor-not-allowed bg-transparent py-1 text-base text-faint placeholder:text-faint outline-none"
              />
              <IconButton
                icon={<SendHorizonal size={18} />}
                label={t("chat.readOnly")}
                variant="ghost"
                size="md"
                disabled
                title={t("chat.readOnly")}
              />
            </div>
          ) : (
            renderComposer(false)
          )}
        </div>
      </div>
    </div>
  );
};
