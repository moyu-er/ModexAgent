import { useState, useRef, useEffect, type FC, type FormEvent, type KeyboardEvent } from "react";
import type { UIMessage } from "../types/events";
import { MessageBubble } from "./MessageBubble";

export interface ChatViewProps {
  messages: UIMessage[];
  isStreaming: boolean;
  isPending: boolean;
  onSend: (content: string) => void;
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

export const ChatView: FC<ChatViewProps> = ({
  messages,
  isStreaming,
  isPending,
  onSend,
  onPause,
  readOnly = false,
  onOpenSidebar,
}) => {
  const [input, setInput] = useState("");
  const bottomRef = useRef<HTMLDivElement | null>(null);
  const taRef = useRef<HTMLTextAreaElement | null>(null);

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
  const canSend = !isBusy && input.trim().length > 0 && !readOnly;

  const submit = (): void => {
    if (readOnly || isBusy) return;
    const trimmed = input.trim();
    if (!trimmed) return;
    onSend(trimmed);
    setInput("");
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
            <MessageBubble key={msg.id} message={msg} />
          ))}
          <div ref={bottomRef} />
        </div>
      </div>

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
            <form onSubmit={handleSubmit} className="composer">
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
