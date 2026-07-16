import { type FC } from "react";
import { Bot, User } from "lucide-react";
import type { TurnBlock, UIMessage } from "../types/events";
import type { AttachmentRecord } from "../types/attachments";
import { MarkdownRenderer } from "./MarkdownRenderer";
import { ReasoningBlock } from "./ReasoningBlock";
import { ToolTraceCard } from "./ToolTraceCard";
import { AttachmentRenderer, type AttachmentView } from "./AttachmentRenderer";
import { TypewriterText } from "../hooks/useTypewriter";
import { formatClock } from "../lib/timezone";
import { attachmentDownloadUrl } from "../lib/api";
import { appendWsParam } from "../lib/url";

// ── Agent-specific label tones ────────────────────────────────────────────

const AGENT_LABEL: Record<string, string> = {
  main: "text-link",
  coding: "text-cat-pools",
};

function agentLabelClass(agentName: string): string {
  return AGENT_LABEL[agentName] ?? "text-mute";
}

export interface MessageBubbleProps {
  message: UIMessage;
  /** Session id — used to resolve attachment download URLs. */
  sessionId?: string | null;
  /** Active workspace (empty/undefined = home) — appended to attachment URLs. */
  workspace?: string;
}

/** Map an inbound AttachmentRecord (3-way kind) to the renderer's 2-way kind. */
function recordToView(rec: AttachmentRecord, sessionId: string | null | undefined, ws: string | undefined): AttachmentView {
  return {
    id: rec.id,
    kind: rec.kind === "image" ? "image" : "file",
    name: rec.name,
    size: rec.size,
    mime: rec.mime,
    downloadUrl: sessionId
      ? attachmentDownloadUrl(sessionId, rec.id, ws)
      : "#",
  };
}

function renderBlock(
  block: TurnBlock,
  index: number,
  isUser: boolean,
  isStreaming: boolean,
  ws: string | undefined,
): JSX.Element {
  if (block.kind === "reasoning") {
    return <ReasoningBlock key={`r-${index}`} reasoning={block.text} />;
  }
  if (block.kind === "tool") {
    return <ToolTraceCard key={`t-${index}`} tool={block.tool} />;
  }
  if (block.kind === "attachment") {
    // Outbound card delta — append the active ws to the bare download_url.
    const withWs = appendWsParam(block.card.download_url, ws);
    return (
      <AttachmentRenderer
        key={`a-${index}`}
        view={{
          id: block.card.attachment_id,
          kind: block.card.kind,
          name: block.card.name,
          size: block.card.size,
          mime: block.card.mime,
          downloadUrl: withWs,
        }}
      />
    );
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
  <div className="flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-hairline-soft text-mute">
    <User size={14} aria-hidden="true" />
  </div>
);

const AssistantAvatar: FC = () => (
  <div className="flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-gradient-to-br from-link to-link-deep text-white">
    <Bot size={14} aria-hidden="true" />
  </div>
);

export const MessageBubble: FC<MessageBubbleProps> = ({ message, sessionId, workspace }) => {
  const isUser = message.role === "user";
  const timeStr = formatTime(message.timestamp);
  const inboundViews = (message.attachments ?? []).map((r) =>
    recordToView(r, sessionId, workspace),
  );

  return (
    <div
      id={`msg-${message.id}`}
      className={`mb-6 flex w-full items-start gap-2 ${isUser ? "flex-row-reverse" : "flex-row"}`}
    >
      {isUser ? <UserAvatar /> : <AssistantAvatar />}

      <div className={`flex min-w-0 flex-1 flex-col ${isUser ? "items-end" : "items-start"}`}>
        <div
          className={`${ isUser ? "bubble-user w-fit max-w-[98%] min-w-[50%]" : "bubble-assistant w-fit max-w-[85%] min-w-[60%]" }`}
        >
          {!isUser && message.agent_name && (
            <div className={`mb-1.5 text-[10px] font-semibold uppercase tracking-wide font-mono ${agentLabelClass(message.agent_name)}`}>
              {message.agent_name}
            </div>
          )}

          <div className="flex flex-col gap-1">
            {(message.blocks ?? []).map((block, index) =>
              renderBlock(block, index, isUser, message.isStreaming, workspace),
            )}
          </div>

          {inboundViews.length > 0 && (
            <div className={`mt-1 flex flex-col gap-1 ${isUser ? "items-end" : "items-start"}`}>
              {inboundViews.map((v) => (
                <AttachmentRenderer key={v.id} view={v} />
              ))}
            </div>
          )}

          {message.isStreaming &&
            (message.blocks ?? []).length === 0 &&
            !isUser && (
              <div className="mt-1 flex items-center gap-2">
                <span className="relative flex h-2 w-2">
                  <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-link opacity-40" />
                  <span className="relative inline-flex h-2 w-2 rounded-full bg-link" />
                </span>
                <span className="text-xs text-mute">thinking</span>
              </div>
            )}
        </div>

        {timeStr && (
          <div className="mt-1 px-1 text-[10px] text-mute">
            {timeStr}
          </div>
        )}
      </div>
    </div>
  );
};
