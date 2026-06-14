import { type FC } from "react";
import type { TurnBlock, UIMessage } from "../types/events";
import { ReasoningBlock } from "./ReasoningBlock";
import { ToolTraceCard } from "./ToolTraceCard";

// ── Agent-specific colors ────────────────────────────────────────────────

const AGENT_COLORS: Record<string, string> = {
  main: "text-brand-400",
  coding: "text-emerald-300",
};

function agentColor(agentName: string): string {
  return AGENT_COLORS[agentName] ?? "text-gray-400";
}

export interface MessageBubbleProps {
  message: UIMessage;
}

function renderBlock(block: TurnBlock, index: number, isStreaming: boolean): JSX.Element {
  if (block.kind === "reasoning") {
    return <ReasoningBlock key={`r-${index}`} reasoning={block.text} />;
  }
  if (block.kind === "tool") {
    return <ToolTraceCard key={`t-${index}`} tool={block.tool} />;
  }
  return (
    <div key={`txt-${index}`} className="whitespace-pre-wrap break-words text-[15px] leading-6">
      {block.text}
      {isStreaming && (
        <span className="ml-1 inline-block h-4 w-0.5 animate-pulse rounded-sm bg-brand-400 align-text-bottom" />
      )}
    </div>
  );
}

function formatTime(timestamp?: number): string | null {
  if (!timestamp) return null;
  const ms = timestamp < 1e12 ? timestamp * 1000 : timestamp;
  const date = new Date(ms);
  const now = new Date();
  const pad = (n: number): string => String(n).padStart(2, "0");
  const timeStr = `${pad(date.getHours())}:${pad(date.getMinutes())}`;
  const isToday =
    date.getFullYear() === now.getFullYear() &&
    date.getMonth() === now.getMonth() &&
    date.getDate() === now.getDate();
  if (isToday) return timeStr;
  const dateStr = `${date.getFullYear()}/${date.getMonth() + 1}/${date.getDate()}`;
  return `${dateStr} ${timeStr}`;
}

export const MessageBubble: FC<MessageBubbleProps> = ({ message }) => {
  const isUser = message.role === "user";
  const timeStr = formatTime(message.timestamp);

  return (
    <div className={`mb-5 flex flex-col ${isUser ? "items-end" : "items-start"}`}>
      <div
        className={`px-5 py-3.5 ${
          isUser
            ? "max-w-[75%] rounded-2xl rounded-br-md bg-brand-600 text-white shadow-md shadow-brand-900/20"
            : "min-w-[55%] max-w-[85%] rounded-2xl rounded-bl-md border border-white/10 bg-ink-800 text-gray-100 shadow-md shadow-black/20"
        }`}
      >
        {!isUser && message.agent_name && (
          <div className={`mb-1.5 text-[10px] font-semibold uppercase tracking-wide ${agentColor(message.agent_name)}`}>
            {message.agent_name}
          </div>
        )}

        <div className="flex flex-col gap-1">
          {(message.blocks ?? []).map((block, index) => renderBlock(block, index, message.isStreaming))}
        </div>

        {message.isStreaming &&
          (message.blocks ?? []).length === 0 &&
          !isUser && (
            <div className="mt-1 flex items-center gap-2">
              <span className="relative flex h-2 w-2">
                <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-brand-400 opacity-40" />
                <span className="relative inline-flex h-2 w-2 rounded-full bg-brand-400" />
              </span>
              <span className="text-xs text-gray-400">thinking</span>
            </div>
          )}
      </div>
      {timeStr && (
        <div className="mt-1.5 px-1 text-[10px] text-gray-500">
          {timeStr}
        </div>
      )}
    </div>
  );
};
