import { describe, expect, it } from "vitest";
import {
  eventsToMessages,
  type AssistantTurnEvent,
  type ModelContentDelta,
  type UIMessage,
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

// ── Regression: liveTail dedup when history already has streaming turn ──────

describe("session re-select: no duplicate when history has streaming turn", () => {
  it("skips liveTail when history already contains a streaming message", () => {
    const historyEvents: AssistantTurnEvent[] = [
      {
        event: "assistant_turn",
        session_id: "abc.main",
        agent_name: "main",
        timestamp: 100,
        blocks: [{ kind: "text", text: "Streaming text from partial buffer" }],
        turn_id: "t1",
        latency_ms: 0,
        is_streaming: true,
      },
    ];
    const history = eventsToMessages(historyEvents);

    const bufferedState = _emptyState();
    bufferedState.isStreaming = false;
    const deltaEnv: DeltaEnvelope = {
      session_id: "abc.main",
      agent_name: "main",
      event_type: "model_content_delta",
      pool: "main",
      parent_session_id: null,
      metadata: { turn_id: "t1" },
      payload: { text: "Streaming text from partial buffer", turn_id: "t1", segment_id: "_text" },
      timestamp: 99,
    };
    const deltaEvent = unwrapEnvelope(deltaEnv) as unknown as ModelContentDelta;
    const buffered = applyServerEvent(
      _emptyState(),
      deltaEvent,
      "abc.main",
      _ref(),
    );
    bufferedState.sessionMessages["abc.main"] = buffered.messages;
    bufferedState.sessionStreaming["abc.main"] = true;

    const buf = bufferedState.sessionMessages["abc.main"] || [];
    const streaming = bufferedState.sessionStreaming["abc.main"] || false;
    const historyHasStreaming = history.some((m) => m.isStreaming);
    const liveTail =
      streaming && !historyHasStreaming
        ? buf.filter((m) => m.isStreaming)
        : [];

    const merged: UIMessage[] = [...history, ...liveTail];

    const streamingMessages = merged.filter((m) => m.isStreaming);
    expect(streamingMessages).toHaveLength(1);
    expect(streamingMessages[0]!.blocks[0]).toMatchObject({
      kind: "text",
      text: "Streaming text from partial buffer",
    });
  });

  it("includes liveTail when history has NO streaming message (fallback)", () => {
    const historyEvents: AssistantTurnEvent[] = [
      {
        event: "assistant_turn",
        session_id: "abc.main",
        agent_name: "main",
        timestamp: 100,
        blocks: [{ kind: "text", text: "Completed turn" }],
        turn_id: "t0",
        latency_ms: 500,
        is_streaming: false,
      },
    ];
    const history = eventsToMessages(historyEvents);

    const deltaEnv: DeltaEnvelope = {
      session_id: "abc.main",
      agent_name: "main",
      event_type: "model_content_delta",
      pool: "main",
      parent_session_id: null,
      metadata: { turn_id: "t1" },
      payload: { text: "Live delta", turn_id: "t1", segment_id: "_text" },
      timestamp: 101,
    };
    const deltaEvent = unwrapEnvelope(deltaEnv) as unknown as ModelContentDelta;
    const buffered = applyServerEvent(_emptyState(), deltaEvent, "abc.main", _ref());

    const buf = buffered.messages;
    const streaming = true;
    const historyHasStreaming = history.some((m) => m.isStreaming);
    const liveTail =
      streaming && !historyHasStreaming
        ? buf.filter((m) => m.isStreaming)
        : [];

    const merged: UIMessage[] = [...history, ...liveTail];

    expect(merged).toHaveLength(2);
    expect(merged[0]!.isStreaming).toBe(false);
    expect(merged[1]!.isStreaming).toBe(true);
  });

  it("does not duplicate text when WS buffer and history both carry the same streaming turn", () => {
    const historyEvents: AssistantTurnEvent[] = [
      {
        event: "assistant_turn",
        session_id: "abc.main",
        agent_name: "main",
        timestamp: 100,
        blocks: [
          { kind: "text", text: "Part A" },
          { kind: "tool", tool: { tool: "sometool", args: {} } },
          { kind: "text", text: "Part B" },
          { kind: "tool", tool: { tool: "sometool", args: {} } },
        ],
        turn_id: "t1",
        latency_ms: 0,
        is_streaming: true,
      },
    ];
    const history = eventsToMessages(historyEvents);

    const wsBufferState = _emptyState();
    for (const text of ["Part A", "Part B"]) {
      const env: DeltaEnvelope = {
        session_id: "abc.main",
        agent_name: "main",
        event_type: "model_content_delta",
        pool: "main",
        parent_session_id: null,
        metadata: { turn_id: "t1" },
        payload: { text, turn_id: "t1", segment_id: "_text" },
        timestamp: 99,
      };
      const evt = unwrapEnvelope(env) as unknown as ModelContentDelta;
      wsBufferState.messages = applyServerEvent(
        wsBufferState,
        evt,
        "abc.main",
        _ref(),
      ).messages;
    }
    wsBufferState.isStreaming = true;

    const buf = wsBufferState.messages;
    const streaming = true;
    const historyHasStreaming = history.some((m) => m.isStreaming);
    const liveTail =
      streaming && !historyHasStreaming
        ? buf.filter((m) => m.isStreaming)
        : [];

    const merged: UIMessage[] = [...history, ...liveTail];

    const allText = merged
      .flatMap((m) => m.blocks.filter((b) => b.kind === "text"))
      .map((b) => (b as { text: string }).text);
    const partACount = allText.filter((t) => t.includes("Part A")).length;
    const partBCount = allText.filter((t) => t.includes("Part B")).length;
    expect(partACount).toBe(1);
    expect(partBCount).toBe(1);
  });
});
