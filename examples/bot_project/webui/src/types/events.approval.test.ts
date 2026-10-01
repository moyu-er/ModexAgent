import { describe, it, expect } from "vitest";
import {
  unwrapEnvelope,
  type ApprovalRequestedEvent,
  type ApprovalResolvedEvent,
  type DeltaEnvelope,
  type UsageSummaryEvent,
} from "./events";

describe("approval / usage envelopes", () => {
  it("unwraps an approval_requested envelope with the streamed card fields", () => {
    const env: DeltaEnvelope = {
      session_id: "s.main",
      agent_name: "main",
      event_type: "approval_requested",
      pool: "main",
      parent_session_id: null,
      metadata: { turn_id: "t1" },
      payload: {
        tool_name: "write_file",
        call_id: "c1",
        turn_id: "t1",
        prompt: "Approval Required [DANGEROUS]\nTool: write_file",
      },
    };
    const ev = unwrapEnvelope(env) as ApprovalRequestedEvent;
    expect(ev.event).toBe("approval_requested");
    expect(ev.call_id).toBe("c1");
    expect(ev.tool_name).toBe("write_file");
    expect(ev.turn_id).toBe("t1");
    expect(ev.prompt).toContain("write_file");
  });

  it("unwraps an approval_resolved envelope with the decision", () => {
    const env: DeltaEnvelope = {
      session_id: "s.main",
      agent_name: "main",
      event_type: "approval_resolved",
      pool: "main",
      parent_session_id: null,
      metadata: {},
      payload: { call_id: "c1", approved: true, turn_id: "t1" },
    };
    const ev = unwrapEnvelope(env) as ApprovalResolvedEvent;
    expect(ev.event).toBe("approval_resolved");
    expect(ev.call_id).toBe("c1");
    expect(ev.approved).toBe(true);
  });

  it("unwraps a usage_summary envelope with the token snapshot", () => {
    const env: DeltaEnvelope = {
      session_id: "s.main",
      agent_name: "main",
      event_type: "usage_summary",
      pool: "main",
      parent_session_id: null,
      metadata: {},
      payload: {
        input_tokens: 10,
        output_tokens: 5,
        reasoning_tokens: 2,
        cache_read_tokens: 7,
        cache_creation_tokens: 0,
        total_tokens: 22,
        turn_id: "t1",
      },
    };
    const ev = unwrapEnvelope(env) as UsageSummaryEvent;
    expect(ev.event).toBe("usage_summary");
    expect(ev.input_tokens).toBe(10);
    expect(ev.output_tokens).toBe(5);
    expect(ev.total_tokens).toBe(22);
    expect(ev.turn_id).toBe("t1");
  });
});
