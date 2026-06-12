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

function renderBlock(block: TurnBlock, isStreaming: boolean): JSX.Element {
  if (block.kind === "reasoning") {
    return <ReasoningBlock key={`r-${block.text.substring(0,20)}`} reasoning={block.text} />;
  }
  if (block.kind === "tool") {
    return <ToolTraceCard key={`t-${block.tool.tool}`} tool={block.tool} />;
  }
  // text block
  return (
    <div
      key={`txt-${block.text.substring(0,20)}`}
      className={`text-sm whitespace-pre-wrap break-words ${
        isStreaming ? "border-r-2 border-green-400 animate-pulse pr-1" : ""
      }`}
    >
      {block.text}
    </div>
  );
}

export const MessageBubble: FC<MessageBubbleProps> = ({ message }) => {
  const isUser = message.role === "user";

  return (
    <div
      className={`flex mb-4 ${isUser ? "justify-end" : "justify-start"}`}
    >
      <div
        className={`max-w-[80%] rounded-lg px-4 py-3 ${
          isUser
            ? "bg-blue-600 text-white rounded-br-sm"
            : "bg-gray-800 text-gray-100 border border-gray-700 rounded-bl-sm"
        }`}
      >
        {!isUser && message.agent_name && (
          <div className={`text-[10px] font-semibold ${agentColor(message.agent_name)} uppercase mb-1 tracking-wide`}>
            {message.agent_name}
          </div>
        )}

        {/* Render blocks in streaming order */}
        <div className="flex flex-col gap-1">
          {(message.blocks ?? []).map((block) => renderBlock(block, message.isStreaming))}
        </div>

        {/* Streaming indicator for empty streaming messages */}
        {message.isStreaming &&
          (message.blocks ?? []).length === 0 &&
          !isUser && (
            <span className="inline-block w-2 h-4 bg-green-400 animate-pulse rounded-sm" />
          )}
      </div>
    </div>
  );
};
