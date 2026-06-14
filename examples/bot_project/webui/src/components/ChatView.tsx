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
}

// Input box is intentionally tall and generous; it grows with content up to
// the cap, then scrolls internally.
const MAX_INPUT_HEIGHT = 630;
const MIN_INPUT_HEIGHT = 126;

// Shared content width keeps the message column and composer aligned.
// Responsive: fills available width, capped so very wide screens stay readable.
const CONTENT_WIDTH = "mx-auto w-full max-w-[1440px]";

export const ChatView: FC<ChatViewProps> = ({
  messages,
  isStreaming,
  isPending,
  onSend,
  onPause,
  readOnly = false,
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

  // The current session is "busy" while it is streaming output or initializing.
  // This drives the send→pause toggle and is bound to the selected session's
  // streaming state, so switching conversations re-evaluates it correctly.
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
    <div className="flex h-full flex-col bg-ink-850">
      {/* Message area */}
      <div className="flex-1 overflow-y-auto">
        <div className={`${CONTENT_WIDTH} px-6 py-8`}>
          {messages.length === 0 && (
            <div className="flex h-[55vh] items-center justify-center">
              <p className="text-sm text-gray-500">
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

      {/* Floating composer — lifted off the bottom edge with elevation */}
      <div className="px-6 pb-10 pt-2">
        <div className={CONTENT_WIDTH}>
          {readOnly ? (
            <div className="composer">
              <input
                type="text"
                disabled
                placeholder="Subagent session — read only"
                className="flex-1 cursor-not-allowed bg-transparent py-1 text-sm text-gray-500 placeholder-gray-600 outline-none"
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
                className={`max-h-[630px] min-h-[126px] flex-1 resize-none overflow-y-auto bg-transparent py-2 text-[15px] leading-6 text-gray-100 outline-none placeholder-gray-500`}
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
    <path d="M12 19V5" />
    <path d="m5 12 7-7 7 7" />
  </svg>
);

const PauseIcon = (): JSX.Element => (
  <svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
    <rect x="6" y="5" width="4" height="14" rx="1.2" />
    <rect x="14" y="5" width="4" height="14" rx="1.2" />
  </svg>
);
