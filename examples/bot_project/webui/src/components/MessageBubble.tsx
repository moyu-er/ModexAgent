import { type FC } from "react";
import type { TurnBlock, UIMessage } from "../types/events";
import { MarkdownRenderer } from "./MarkdownRenderer";
import { ReasoningBlock } from "./ReasoningBlock";
import { ToolTraceCard } from "./ToolTraceCard";
import { TypewriterText } from "../hooks/useTypewriter";
import { formatClock } from "../lib/timezone";

// ── Agent-specific colors ────────────────────────────────────────────────

const AGENT_COLORS: Record<string, string> = {
  main: "text-ai-brand-light dark:text-ai-brand-dark",
  coding: "text-success-light dark:text-success-dark",
};

function agentColor(agentName: string): string {
  return AGENT_COLORS[agentName] ?? "text-text-secondary-light dark:text-text-secondary-dark";
}

export interface MessageBubbleProps {
  message: UIMessage;
}

function renderBlock(
  block: TurnBlock,
  index: number,
  isUser: boolean,
  isStreaming: boolean,
): JSX.Element {
  if (block.kind === "reasoning") {
    return <ReasoningBlock key={`r-${index}`} reasoning={block.text} />;
  }
  if (block.kind === "tool") {
    return <ToolTraceCard key={`t-${index}`} tool={block.tool} />;
  }

  if (isUser) {
    return (
      <div key={`txt-${index}`} className="whitespace-pre-wrap break-words text-[15px] leading-relaxed">
        {block.text}
      </div>
    );
  }

  if (isStreaming) {
    return (
      <div key={`txt-${index}`} className="text-[15px] leading-relaxed">
        <TypewriterText text={block.text} isStreaming={isStreaming} />
      </div>
    );
  }

  return <MarkdownRenderer key={`txt-${index}`} content={block.text} />;
}

function formatTime(timestamp?: number): string | null {
  if (!timestamp) return null;
  return formatClock(timestamp);
}

const UserAvatar: FC = () => (
  <div className="flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-sidebar-hover-light text-text-secondary-light dark:bg-sidebar-hover-dark dark:text-text-secondary-dark">
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M19 21v-2a4 4 0 0 0-4-4H9a4 4 0 0 0-4 4v2" />
      <circle cx="12" cy="7" r="4" />
    </svg>
  </div>
);

const AssistantAvatar: FC = () => (
  <div className="flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-ai-brand-light text-[#ffffff] dark:bg-ai-brand-dark">
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="m12 3-1.912 5.813a2 2 0 0 1-1.275 1.275L3 12l5.813 1.912a2 2 0 0 1 1.275 1.275L12 21l1.912-5.813a2 2 0 0 1 1.275-1.275L21 12l-5.813-1.912a2 2 0 0 1-1.275-1.275L12 3Z" />
      <path d="M5 3v4" />
      <path d="M9 5H5" />
      <path d="M19 16v4" />
      <path d="M15 18h4" />
    </svg>
  </div>
);

export const MessageBubble: FC<MessageBubbleProps> = ({ message }) => {
  const isUser = message.role === "user";
  const timeStr = formatTime(message.timestamp);

  return (
    <div className={`mb-6 flex w-full items-start gap-2 ${isUser ? "flex-row-reverse" : "flex-row"}`}>
      {isUser ? <UserAvatar /> : <AssistantAvatar />}

      <div className={`flex min-w-0 flex-1 flex-col ${isUser ? "items-end" : "items-start"}`}>
        <div
          className={`shadow-sm ${
            isUser
              ? "w-fit max-w-[98%] min-w-[50%] rounded-xl rounded-br-md bg-user-bubble-light py-6 px-6 text-user-bubble-text-light dark:bg-user-bubble-dark dark:text-user-bubble-text-dark"
              : "w-fit max-w-[85%] min-w-[60%] rounded-xl rounded-bl-md bg-ai-bubble-light p-4 text-ai-bubble-text-light dark:bg-ai-bubble-dark dark:text-ai-bubble-text-dark"
          }`}
        >
          {!isUser && message.agent_name && (
            <div className={`mb-1.5 text-[10px] font-semibold uppercase tracking-wide ${agentColor(message.agent_name)}`}>
              {message.agent_name}
            </div>
          )}

          <div className="flex flex-col gap-1">
            {(message.blocks ?? []).map((block, index) => renderBlock(block, index, isUser, message.isStreaming))}
          </div>

          {message.isStreaming &&
            (message.blocks ?? []).length === 0 &&
            !isUser && (
              <div className="mt-1 flex items-center gap-2">
                <span className="relative flex h-2 w-2">
                  <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-ai-brand-light opacity-40 dark:bg-ai-brand-dark" />
                  <span className="relative inline-flex h-2 w-2 rounded-full bg-ai-brand-light dark:bg-ai-brand-dark" />
                </span>
                <span className="text-xs text-text-secondary-light dark:text-text-secondary-dark">thinking</span>
              </div>
            )}
        </div>

        {timeStr && (
          <div className="mt-1 px-1 text-[10px] text-text-secondary-light dark:text-text-secondary-dark">
            {timeStr}
          </div>
        )}
      </div>
    </div>
  );
};
