import { describe, expect, it } from "vitest";
import {
  applyServerEvent,
  clearPendingApproval,
  type StreamState,
} from "./useWebUIStream.reducer";
import {
  unwrapEnvelope,
  type DeltaEnvelope,
  type ServerEventUnion,
} from "../types/events";

describe("unwrapEnvelope", () => {
  it("splits event_type → event and payload → flat fields", () => {
    const env: DeltaEnvelope = {
      session_id: "conv.reviewer.aa11",
      agent_name: "reviewer",
      event_type: "tool_call_start",
      pool: "testpool",
      parent_session_id: "conv.testpool",
      metadata: { turn_id: "turn_1" },
      payload: { tool: "read", args: { path: "x" }, turn_id: "turn_1" },
      timestamp: 1781300000000,
    };
    const event = unwrapEnvelope(env);
    expect(event.event).toBe("tool_call_start");
    expect(event.session_id).toBe("conv.reviewer.aa11");
    expect(event.agent_name).toBe("reviewer");
    expect((event as unknown as Record<string, unknown>).tool).toBe("read");
    expect((event as unknown as Record<string, unknown>)._pool).toBe("testpool");
    expect((event as unknown as Record<string, unknown>)._parent_session_id).toBe("conv.testpool");
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
  return {
    messages: [],
    isStreaming: false,
    sessionMessages: {},
    sessionStreaming: {},
    todos: {},
    pendingApprovals: {},
  };
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
        session_id: "conv-a.subagent",
        agent_name: "subagent",
        text: "hello from subagent",
        turn_id: "turn_1",
      },
      "conv-a.main",
      ref,
    );
    // Selected session unchanged
    expect(state.messages).toHaveLength(0);
    // Subagent session buffered
    expect(state.sessionMessages["conv-a.subagent"]).toHaveLength(1);
    expect(state.sessionStreaming["conv-a.subagent"]).toBe(true);
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

describe("applyServerEvent streaming stability", () => {
  it("keeps streaming true across tool_call_end (turn not finished)", () => {
    const ref = { current: null as string | null };
    let state = applyServerEvent(
      emptyState(),
      {
        event: "tool_call_start",
        session_id: "conv.main",
        agent_name: "main",
        tool: "read",
        args: { path: "x" },
        turn_id: "turn_1",
      },
      "conv.main",
      ref,
    );
    expect(state.isStreaming).toBe(true);

    state = applyServerEvent(
      state,
      {
        event: "tool_call_end",
        session_id: "conv.main",
        agent_name: "main",
        tool: "read",
        result_summary: "ok",
        turn_id: "turn_1",
      },
      "conv.main",
      ref,
    );
    // Tool finished but the turn continues — must stay busy so the
    // send/pause toggle doesn't flicker mid-turn.
    expect(state.isStreaming).toBe(true);
  });

  it("mirrors the selected session's streaming flag into sessionStreaming", () => {
    const ref = { current: null as string | null };
    const state = applyServerEvent(
      emptyState(),
      {
        event: "model_content_delta",
        session_id: "conv.main",
        agent_name: "main",
        text: "hi",
        turn_id: "turn_1",
      },
      "conv.main",
      ref,
    );
    // After streaming on the selected session, switching away and back must
    // remember it is still busy — the per-session map holds that truth.
    expect(state.sessionStreaming["conv.main"]).toBe(true);
  });
});

describe("applyServerEvent control notices", () => {
  it("surfaces a 'content' notice as a visible non-streaming message", () => {
    // Backend control notices (e.g. "⏹ Agent turn stopped.",
    // "No running agent turn to stop.") arrive as event_type="content"
    // envelopes (WebSocketOutputAdapter.send wraps OutputMessage as a
    // content DeltaEnvelope). Without handling, the pause button gave no
    // feedback when there was no active turn to cancel.
    const ref = { current: null as string | null };
    const state = applyServerEvent(
      emptyState(),
      {
        event: "content",
        session_id: "conv.main",
        agent_name: "main",
        text: "No running agent turn to stop.",
        // content envelopes carry the text under `text` (payload {text})
      } as unknown as Parameters<typeof applyServerEvent>[1],
      "conv.main",
      ref,
    );
    expect(state.messages).toHaveLength(1);
    const msg = state.messages[0]!;
    expect(msg.role).toBe("system");
    expect(msg.isStreaming).toBe(false);
    expect(msg.blocks[0]).toMatchObject({
      kind: "text",
      text: "No running agent turn to stop.",
    });
    // A notice must not flip the streaming flag on.
    expect(state.isStreaming).toBe(false);
  });
});

describe("applyServerEvent approval_request", () => {
  it("stores approval_request into pendingApprovals keyed by session", () => {
    const ref = { current: null as string | null };
    const state: StreamState = {
      messages: [],
      isStreaming: false,
      sessionMessages: {},
      sessionStreaming: {},
      todos: {},
      pendingApprovals: {},
    };
    const ev = {
      event: "approval_request",
      session_id: "s.main",
      agent_name: "main",
      timestamp: 1,
      tool_call_id: "c1",
      tool_name: "write_file",
      tier: "dangerous",
      arguments: { path: "a" },
      status: "pending",
    } as unknown as ServerEventUnion;
    const next = applyServerEvent(state, ev, "s.main", ref);
    expect(next.pendingApprovals["s.main"]).toHaveLength(1);
    expect(next.pendingApprovals["s.main"]![0]!.tool_call_id).toBe("c1");
  });

  it("dedupes a repeated approval_request by tool_call_id", () => {
    const ref = { current: null as string | null };
    const state: StreamState = {
      messages: [],
      isStreaming: false,
      sessionMessages: {},
      sessionStreaming: {},
      todos: {},
      pendingApprovals: {
        "s.main": [
          {
            tool_call_id: "c1",
            tool_name: "write_file",
            tier: "dangerous",
            arguments: {},
            status: "pending",
          },
        ],
      },
    };
    const ev = {
      event: "approval_request",
      session_id: "s.main",
      agent_name: "main",
      tool_call_id: "c1",
      tool_name: "write_file",
      tier: "dangerous",
      arguments: {},
      status: "pending",
    } as unknown as ServerEventUnion;
    const next = applyServerEvent(state, ev, "s.main", ref);
    expect(next.pendingApprovals["s.main"]).toHaveLength(1);
    expect(next).toBe(state); // unchanged (dedup returns same reference)
  });

  it("stores approval for a non-selected session without buffering into messages", () => {
    // Approvals key by their own session_id and must NOT be routed through
    // the session-routing branch (which would buffer into sessionMessages).
    const ref = { current: null as string | null };
    const state: StreamState = {
      messages: [],
      isStreaming: false,
      sessionMessages: {},
      sessionStreaming: {},
      todos: {},
      pendingApprovals: {},
    };
    const ev = {
      event: "approval_request",
      session_id: "s.subagent",
      agent_name: "subagent",
      tool_call_id: "c9",
      tool_name: "edit_file",
      tier: "dangerous",
      arguments: {},
      status: "pending",
    } as unknown as ServerEventUnion;
    const next = applyServerEvent(state, ev, "s.main", ref);
    expect(next.pendingApprovals["s.subagent"]).toHaveLength(1);
    expect(next.sessionMessages["s.subagent"]).toBeUndefined();
  });
});

describe("clearPendingApproval", () => {
  it("removes a decided approval by tool_call_id", () => {
    const state: StreamState = {
      messages: [],
      isStreaming: false,
      sessionMessages: {},
      sessionStreaming: {},
      todos: {},
      pendingApprovals: {
        "s.main": [
          {
            tool_call_id: "c1",
            tool_name: "write_file",
            tier: "dangerous",
            arguments: {},
            status: "pending",
          },
          {
            tool_call_id: "c2",
            tool_name: "edit_file",
            tier: "dangerous",
            arguments: {},
            status: "pending",
          },
        ],
      },
    };
    const next = clearPendingApproval(state, "s.main", "c1");
    expect(next.pendingApprovals["s.main"]).toHaveLength(1);
    expect(next.pendingApprovals["s.main"]![0]!.tool_call_id).toBe("c2");
  });

  it("preserves other sessions' approvals", () => {
    const state: StreamState = {
      messages: [],
      isStreaming: false,
      sessionMessages: {},
      sessionStreaming: {},
      todos: {},
      pendingApprovals: {
        "s.main": [
          {
            tool_call_id: "c1",
            tool_name: "write_file",
            tier: "dangerous",
            arguments: {},
            status: "pending",
          },
        ],
        "s.subagent": [
          {
            tool_call_id: "c2",
            tool_name: "edit_file",
            tier: "dangerous",
            arguments: {},
            status: "pending",
          },
        ],
      },
    };
    const next = clearPendingApproval(state, "s.main", "c1");
    expect(next.pendingApprovals["s.main"]).toHaveLength(0);
    expect(next.pendingApprovals["s.subagent"]).toHaveLength(1);
  });
});
