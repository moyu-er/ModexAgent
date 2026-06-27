import { describe, it, expect } from "vitest";
import { unwrapEnvelope, type ApprovalRequestEvent, type DeltaEnvelope } from "./events";

describe("approval_request envelope", () => {
  it("unwraps into an ApprovalRequestEvent with view fields", () => {
    const env: DeltaEnvelope = {
      session_id: "s.main",
      agent_name: "main",
      event_type: "approval_request",
      pool: "main",
      parent_session_id: null,
      metadata: {},
      payload: {
        tool_call_id: "c1",
        tool_name: "write_file",
        tier: "dangerous",
        arguments: { path: "a" },
        status: "pending",
      },
    };
    const ev = unwrapEnvelope(env) as ApprovalRequestEvent;
    expect(ev.event).toBe("approval_request");
    expect(ev.tool_call_id).toBe("c1");
    expect(ev.tool_name).toBe("write_file");
    expect(ev.tier).toBe("dangerous");
    expect(ev.arguments).toEqual({ path: "a" });
    expect(ev.status).toBe("pending");
  });
});
