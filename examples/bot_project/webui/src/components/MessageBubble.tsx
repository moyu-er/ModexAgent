import { type FC } from "react";
import type { TurnBlock, UIMessage } from "../types/events";
import { ReasoningBlock } from "./ReasoningBlock";
import { ToolTraceCard } from "./ToolTraceCard";

// ── Agent-specific colors ────────────────────────────────────────────────

const AGENT_COLORS: Record<string, string> = {
  main: "text-blue-400",
  coding: "text-green-400",
};

function agentColor(agentName: string): string {
  return AGENT_COLORS[agentName] ?? "text-gray-500";
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
  // text block
  return (
    <div key={`txt-${index}`} className="text-sm whitespace-pre-wrap break-words">
      {block.text}
      {isStreaming && (
        <span className="inline-block w-0.5 h-4 ml-0.5 align-text-bottom bg-green-400 animate-pulse rounded-sm" />
      )}
    </div>
  );
}

function formatTime(timestamp?: number): string | null {
  if (!timestamp) return null;
  // Auto-detect: float seconds (< 1e12) or int milliseconds (>= 1e12).
  const ms = timestamp < 1e12 ? timestamp * 1000 : timestamp;
  return new Date(ms).toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
  });
}

export const MessageBubble: FC<MessageBubbleProps> = ({ message }) => {
  const isUser = message.role === "user";
  const timeStr = formatTime(message.timestamp);

  return (
    <div
      className={`flex flex-col mb-4 ${isUser ? "items-end" : "items-start"}`}
    >
      <div
        className={`rounded-lg px-4 py-3 ${
          isUser
            ? "max-w-[75%] bg-blue-600 text-white rounded-br-sm"
            : "min-w-[55%] max-w-[85%] bg-gray-800 text-gray-100 border border-gray-700 rounded-bl-sm"
        }`}
      >
        {!isUser && message.agent_name && (
          <div className={`text-[10px] font-semibold ${agentColor(message.agent_name)} uppercase mb-1 tracking-wide`}>
            {message.agent_name}
          </div>
        )}

        {/* Render blocks in streaming order */}
        <div className="flex flex-col gap-1">
          {(message.blocks ?? []).map((block, index) => renderBlock(block, index, message.isStreaming))}
        </div>

        {/* Streaming indicator for empty streaming messages */}
        {message.isStreaming &&
          (message.blocks ?? []).length === 0 &&
          !isUser && (
            <span className="inline-block w-2 h-4 bg-green-400 animate-pulse rounded-sm" />
          )}
      </div>
      {timeStr && (
        <div className={`text-[10px] text-gray-500 mt-1 px-1`}>
          {timeStr}
        </div>
      )}
    </div>
  );
};
