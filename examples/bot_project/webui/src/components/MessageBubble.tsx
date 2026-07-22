import { type FC } from "react";
import { Bot, User } from "lucide-react";
import type { TurnBlock, UIMessage } from "../types/events";
import type { AttachmentRecord } from "../types/attachments";
import { MarkdownRenderer } from "./MarkdownRenderer";
import { ReasoningBlock } from "./ReasoningBlock";
import { ToolTraceCard } from "./ToolTraceCard";
import { AttachmentRenderer, type AttachmentView } from "./AttachmentRenderer";
import { formatClock } from "../lib/timezone";
import { attachmentDownloadUrl } from "../lib/api";
import { appendWsParam } from "../lib/url";
import { useT } from "../i18n";

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
      <div key={`txt-${index}`} className="whitespace-pre-wrap break-words text-md leading-relaxed">
        {block.text}
      </div>
    );
  }

  if (isStreaming) {
    return (
      <div key={`txt-${index}`} className="whitespace-pre-wrap break-words text-md leading-relaxed">
        {block.text}
        <span className="ml-0.5 inline-block h-4 w-0.5 animate-pulse rounded-sm bg-link align-text-bottom" aria-hidden="true" />
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

/** Assistant rail mark: the Bot glyph (matches the chat header's agent
 *  indicator), in a brand-tinted circular badge so it reads as an avatar
 *  rather than a bare logo. */
const AssistantRailMark: FC = () => (
  <div
    className="mt-1 flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-brand-soft text-brand"
    aria-hidden="true"
  >
    <Bot size={15} />
  </div>
);

/** Typing indicator (§6): three staggered brand dots + a text label. */
const TypingDots: FC<{ label: string }> = ({ label }) => (
  <div className="mt-1 flex items-center gap-2" role="status" aria-label={label}>
    <span className="typing-dots" aria-hidden="true">
      <span className="typing-dot" />
      <span className="typing-dot" />
      <span className="typing-dot" />
    </span>
    <span className="text-xs text-mute">{label}</span>
  </div>
);

export const MessageBubble: FC<MessageBubbleProps> = ({ message, sessionId, workspace }) => {
  const t = useT();
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
      {isUser ? <UserAvatar /> : <AssistantRailMark />}

      <div className={`flex min-w-0 flex-1 flex-col ${isUser ? "items-end" : "items-start"}`}>
        {/* Assistant prose: subtle brand-tinted surface with a left accent
            rail (§6) — distinct from canvas, not a full bubble. User
            messages keep the branded bubble (surface tokens in CSS). */}
        <div className={isUser ? "bubble-user" : "bubble-assistant"}>
          {!isUser && message.agent_name && (
            <div className={`mb-1.5 chat-label ${agentLabelClass(message.agent_name)}`}>
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
            !isUser && <TypingDots label={t("message.thinking")} />}
        </div>

        {timeStr && (
          <div className="mt-1 px-1 text-xs text-mute">
            {timeStr}
          </div>
        )}
      </div>
    </div>
  );
};
