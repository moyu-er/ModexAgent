import { useState, useRef, useEffect, type FC, type FormEvent, type KeyboardEvent } from "react";
import type { UIMessage } from "../types/events";
import { MessageBubble } from "./MessageBubble";

export interface ChatViewProps {
  messages: UIMessage[];
  isStreaming: boolean;
  isPending: boolean;
  onSend: (content: string) => void;
  readOnly?: boolean;
}

export const ChatView: FC<ChatViewProps> = ({
  messages,
  isStreaming,
  isPending,
  onSend,
  readOnly = false,
}) => {
  const [input, setInput] = useState("");
  const bottomRef = useRef<HTMLDivElement | null>(null);

  // Auto-scroll to bottom when messages change
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  const submit = (): void => {
    if (readOnly) return;
    const trimmed = input.trim();
    if (!trimmed || isStreaming || isPending) {
      return;
    }
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

  return (
    <div className="flex flex-col h-full">
      {/* Message area */}
      <div className="flex-1 overflow-y-auto p-4">
        {messages.length === 0 && (
          <div className="flex items-center justify-center h-full">
            <p className="text-gray-600 text-sm">
              Select a conversation to start chatting
            </p>
          </div>
        )}
        {messages.map((msg) => (
          <MessageBubble key={msg.id} message={msg} />
        ))}
        <div ref={bottomRef} />
      </div>

      {/* Input area */}
      <div className="border-t border-gray-800 p-4">
        {readOnly ? (
          <div className="flex gap-2">
            <input
              type="text"
              disabled
              placeholder="Subagent session — read only"
              className="flex-1 bg-gray-800/50 border border-gray-700 rounded-lg px-3 py-2 text-sm text-gray-500 placeholder-gray-600 cursor-not-allowed"
            />
            <button
              type="button"
              disabled
              className="px-4 py-2 bg-gray-700 text-gray-500 text-sm font-medium rounded-lg cursor-not-allowed"
            >
              Send
            </button>
          </div>
        ) : (
          <form onSubmit={handleSubmit} className="flex gap-2">
            <textarea
              value={input}
              onChange={(e): void => setInput(e.target.value)}
              onKeyDown={handleKeyDown}
              placeholder={
                isPending ? "Initializing session..." : isStreaming ? "Waiting for response..." : "Type a message..."
              }
              disabled={isStreaming || isPending}
              rows={6}
              className="flex-1 bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-sm text-gray-100 placeholder-gray-500 resize-none overflow-y-auto max-h-40 focus:outline-none focus:border-blue-500 focus:ring-1 focus:ring-blue-500 disabled:opacity-50"
            />
            <button
              type="submit"
              disabled={isStreaming || isPending || !input.trim()}
              className="px-4 py-2 bg-blue-600 text-white text-sm font-medium rounded-lg hover:bg-blue-500 transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
            >
              Send
            </button>
          </form>
        )}
      </div>
    </div>
  );
};
