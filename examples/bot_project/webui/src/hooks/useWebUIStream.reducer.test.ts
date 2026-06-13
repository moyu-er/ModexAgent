import { describe, expect, it } from "vitest";
import { applyServerEvent, type StreamState } from "./useWebUIStream.reducer";
import { unwrapEnvelope, type DeltaEnvelope } from "../types/events";

describe("unwrapEnvelope", () => {
  it("splits event_type → event and payload → flat fields", () => {
    const env: DeltaEnvelope = {
      session_id: "conv.reviewer.aa11",
      agent_name: "reviewer",
      event_type: "tool_call_start",
      pool: "coding",
      parent_session_id: "conv.coding",
      metadata: { turn_id: "turn_1" },
      payload: { tool: "read", args: { path: "x" }, turn_id: "turn_1" },
      timestamp: 1781300000000,
    };
    const event = unwrapEnvelope(env);
    expect(event.event).toBe("tool_call_start");
    expect(event.session_id).toBe("conv.reviewer.aa11");
    expect(event.agent_name).toBe("reviewer");
    expect((event as unknown as Record<string, unknown>).tool).toBe("read");
    expect((event as unknown as Record<string, unknown>)._pool).toBe("coding");
    expect((event as unknown as Record<string, unknown>)._parent_session_id).toBe("conv.coding");
    expect((event as unknown as Record<string, unknown>)._metadata).toEqual({ turn_id: "turn_1" });
  });

  it("preserves timestamp from envelope", () => {
    const env: DeltaEnvelope = {
      session_id: "x.main",
      agent_name: "main",
      event_type: "user_message",
      pool: "",
      parent_session_id: null,
      metadata: {},
      payload: { content: "hi" },
      timestamp: 1781300000000,
    };
    const event = unwrapEnvelope(env);
    expect(event.timestamp).toBe(1781300000000);
  });

  it("handles missing metadata/parent gracefully", () => {
    const env: DeltaEnvelope = {
      session_id: "x.main",
      agent_name: "main",
      event_type: "model_content_delta",
      pool: "main",
      parent_session_id: null,
      metadata: {},
      payload: { text: "hi", turn_id: "t1" },
      timestamp: 1781300000000,
    };
    const event = unwrapEnvelope(env);
    expect(event.event).toBe("model_content_delta");
    expect((event as unknown as Record<string, unknown>).text).toBe("hi");
  });
});

function emptyState(): StreamState {
  return { messages: [], isStreaming: false, sessionMessages: {}, sessionStreaming: {} };
}

describe("applyServerEvent session isolation", () => {
  it("buffers model_content_delta for a different session in sessionMessages", () => {
    const ref = { current: null as string | null };
    const state = applyServerEvent(
      emptyState(),
      {
        event: "model_content_delta",
        session_id: "conv-a.main",
        agent_name: "main",
        text: "hello from A",
        turn_id: "turn_1",
      },
      "conv-b.main",
      ref,
    );
    // Selected session stays empty
    expect(state.messages).toHaveLength(0);
    expect(state.isStreaming).toBe(false);
    // Buffered under the other session's id
    expect(state.sessionMessages["conv-a.main"]).toHaveLength(1);
    expect(state.sessionStreaming["conv-a.main"]).toBe(true);
  });

  it("renders model_content_delta for the current session", () => {
    const ref = { current: null as string | null };
    const state = applyServerEvent(
      emptyState(),
      {
        event: "model_content_delta",
        session_id: "conv-a.main",
        agent_name: "main",
        text: "hello from A",
        turn_id: "turn_1",
      },
      "conv-a.main",
      ref,
    );
    expect(state.messages).toHaveLength(1);
    expect(state.isStreaming).toBe(true);
  });

  it("buffers events from subagent under same conversation", () => {
    const ref = { current: null as string | null };
    const state = applyServerEvent(
      emptyState(),
      {
        event: "model_content_delta",
        session_id: "conv-a.coding",
        agent_name: "coding",
        text: "hello from coding",
        turn_id: "turn_1",
      },
      "conv-a.main",
      ref,
    );
    // Selected session unchanged
    expect(state.messages).toHaveLength(0);
    // Subagent session buffered
    expect(state.sessionMessages["conv-a.coding"]).toHaveLength(1);
    expect(state.sessionStreaming["conv-a.coding"]).toBe(true);
  });

  it("buffers turn_end for a different session", () => {
    const ref = { current: null as string | null };
    let state = applyServerEvent(
      emptyState(),
      {
        event: "model_content_delta",
        session_id: "conv-b.main",
        agent_name: "main",
        text: "streaming...",
        turn_id: "turn_1",
      },
      "conv-a.main",
      ref,
    );
    expect(state.sessionStreaming["conv-b.main"]).toBe(true);

    state = applyServerEvent(
      state,
      {
        event: "turn_end",
        session_id: "conv-b.main",
        agent_name: "main",
        turn_id: "turn_1",
        latency_ms: 0,
      },
      "conv-a.main",
      ref,
    );
    // Selected session unchanged, but buffer streaming flag cleared
    expect(state.isStreaming).toBe(false);
    expect(state.sessionStreaming["conv-b.main"]).toBe(false);
  });

  it("applies turn_end for the current session", () => {
    const ref = { current: null as string | null };
    let state = applyServerEvent(
      emptyState(),
      {
        event: "model_content_delta",
        session_id: "conv-a.main",
        agent_name: "main",
        text: "streaming...",
        turn_id: "turn_1",
      },
      "conv-a.main",
      ref,
    );
    expect(state.isStreaming).toBe(true);

    state = applyServerEvent(
      state,
      {
        event: "turn_end",
        session_id: "conv-a.main",
        agent_name: "main",
        turn_id: "turn_1",
        latency_ms: 0,
      },
      "conv-a.main",
      ref,
    );
    expect(state.isStreaming).toBe(false);
  });
});
