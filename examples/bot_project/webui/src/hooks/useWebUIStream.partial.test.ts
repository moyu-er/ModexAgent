import { describe, expect, it } from "vitest";
import {
  eventsToMessages,
  type AssistantTurnEvent,
  type ModelContentDelta,
  unwrapEnvelope,
  type DeltaEnvelope,
} from "../types/events";
import { applyServerEvent, type StreamState } from "./useWebUIStream.reducer";

function _emptyState(messages: StreamState["messages"] = []): StreamState {
  return {
    messages,
    isStreaming: false,
    sessionMessages: {},
    sessionStreaming: {},
    todos: {},
    pendingApprovals: {},
  };
}

const _ref = (): { current: string | null } => ({ current: null });

describe("eventsToMessages — is_streaming field", () => {
  it("marks assistant_turn with is_streaming=true as streaming", () => {
    const events: AssistantTurnEvent[] = [
      {
        event: "assistant_turn",
        session_id: "abc.main",
        agent_name: "main",
        timestamp: 100,
        blocks: [{ kind: "text", text: "Partial answer" }],
        turn_id: "t1",
        latency_ms: 0,
        is_streaming: true,
      },
    ];
    const messages = eventsToMessages(events);
    expect(messages).toHaveLength(1);
    expect(messages[0]!.isStreaming).toBe(true);
    expect(messages[0]!.blocks[0]).toEqual({ kind: "text", text: "Partial answer" });
  });

  it("marks assistant_turn without is_streaming as not streaming (backward compat)", () => {
    const events: AssistantTurnEvent[] = [
      {
        event: "assistant_turn",
        session_id: "abc.main",
        agent_name: "main",
        timestamp: 100,
        blocks: [{ kind: "text", text: "Complete answer" }],
        turn_id: "t1",
        latency_ms: 500,
      },
    ];
    const messages = eventsToMessages(events);
    expect(messages[0]!.isStreaming).toBe(false);
  });

  it("marks assistant_turn with is_streaming=false as not streaming", () => {
    const events: AssistantTurnEvent[] = [
      {
        event: "assistant_turn",
        session_id: "abc.main",
        agent_name: "main",
        timestamp: 100,
        blocks: [{ kind: "text", text: "Done" }],
        turn_id: "t1",
        latency_ms: 500,
        is_streaming: false,
      },
    ];
    const messages = eventsToMessages(events);
    expect(messages[0]!.isStreaming).toBe(false);
  });
});

describe("refresh-mid-stream: partial history + live WS delta merge", () => {
  it("appends WS model_content_delta to a streaming message from history", () => {
    const historyEvents: AssistantTurnEvent[] = [
      {
        event: "assistant_turn",
        session_id: "abc.main",
        agent_name: "main",
        timestamp: 100,
        blocks: [{ kind: "text", text: "Hello" }],
        turn_id: "t1",
        latency_ms: 0,
        is_streaming: true,
      },
    ];
    let state = _emptyState(eventsToMessages(historyEvents));
    state.isStreaming = true;

    const deltaEnv: DeltaEnvelope = {
      session_id: "abc.main",
      agent_name: "main",
      event_type: "model_content_delta",
      pool: "main",
      parent_session_id: null,
      metadata: { turn_id: "t1" },
      payload: { text: " world", turn_id: "t1", segment_id: "_text" },
      timestamp: 101,
    };
    const deltaEvent = unwrapEnvelope(deltaEnv) as unknown as ModelContentDelta;
    state = applyServerEvent(state, deltaEvent, "abc.main", _ref());

    const lastMsg = state.messages[state.messages.length - 1]!;
    expect(lastMsg.isStreaming).toBe(true);
    const textBlock = lastMsg.blocks.find((b) => b.kind === "text") as { text: string } | undefined;
    expect(textBlock?.text).toBe("Hello world");
  });

  it("does not modify a completed (non-streaming) message when a stale delta arrives", () => {
    const historyEvents: AssistantTurnEvent[] = [
      {
        event: "assistant_turn",
        session_id: "abc.main",
        agent_name: "main",
        timestamp: 100,
        blocks: [{ kind: "text", text: "Complete" }],
        turn_id: "t1",
        latency_ms: 500,
        is_streaming: false,
      },
    ];
    let state = _emptyState(eventsToMessages(historyEvents));

    const deltaEnv: DeltaEnvelope = {
      session_id: "abc.main",
      agent_name: "main",
      event_type: "model_content_delta",
      pool: "main",
      parent_session_id: null,
      metadata: { turn_id: "t1" },
      payload: { text: " stale", turn_id: "t1", segment_id: "_text" },
      timestamp: 101,
    };
    const deltaEvent = unwrapEnvelope(deltaEnv) as unknown as ModelContentDelta;
    state = applyServerEvent(state, deltaEvent, "abc.main", _ref());

    const completedMsg = state.messages.find(
      (m) => m.blocks.some((b) => b.kind === "text" && (b as { text: string }).text === "Complete"),
    );
    expect(completedMsg).toBeDefined();
    expect(completedMsg!.isStreaming).toBe(false);
  });
});
