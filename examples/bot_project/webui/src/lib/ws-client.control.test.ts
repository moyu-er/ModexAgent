// ws-client control channel: flat `{"type": "sessions_changed", ...}`
// messages (PA-02). Control notifications are NOT chat events — they must be
// intercepted before onEvent (never enter the chat reducer / transcript) and
// routed to a registered control handler.

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { WebSocketClient, isSessionsChangedMessage } from "./ws-client";

interface FakeSocket {
  readyState: number;
  onopen: ((ev: Event) => void) | null;
  onclose: ((ev: CloseEvent) => void) | null;
  onmessage: ((ev: MessageEvent) => void) | null;
  onerror: ((ev: Event) => void) | null;
  close: () => void;
}

let created: FakeSocket[] = [];

const makeFake = (): FakeSocket => {
  const socket: FakeSocket = {
    readyState: 1,
    onopen: null,
    onclose: null,
    onmessage: null,
    onerror: null,
    close: (): void => {
      socket.readyState = 3;
    },
  };
  created.push(socket);
  return socket;
};

const installWebSocket = (): void => {
  const FakeCtor = function (): FakeSocket {
    return makeFake();
  } as unknown as { new (): FakeSocket; OPEN: number; CLOSED: number };
  FakeCtor.OPEN = 1;
  FakeCtor.CLOSED = 3;
  vi.stubGlobal("WebSocket", FakeCtor);
};

const receive = (socket: FakeSocket, data: unknown): void => {
  socket.onmessage?.({ data: JSON.stringify(data) } as MessageEvent<string>);
};

describe("isSessionsChangedMessage", () => {
  it("accepts a flat sessions_changed message with a workspace path", () => {
    expect(
      isSessionsChangedMessage({ type: "sessions_changed", workspace: "/ws/a" }),
    ).toBe(true);
  });

  it("accepts a message whose workspace is the empty string (home)", () => {
    expect(
      isSessionsChangedMessage({ type: "sessions_changed", workspace: "" }),
    ).toBe(true);
  });

  it("rejects other types, missing workspace, and non-objects", () => {
    expect(isSessionsChangedMessage({ type: "graph_event" })).toBe(false);
    expect(isSessionsChangedMessage({ type: "sessions_changed" })).toBe(false);
    expect(isSessionsChangedMessage({ type: "sessions_changed", workspace: 5 })).toBe(false);
    expect(isSessionsChangedMessage(null)).toBe(false);
    expect(isSessionsChangedMessage("sessions_changed")).toBe(false);
  });
});

describe("WebSocketClient control routing", () => {
  beforeEach(() => {
    created = [];
    installWebSocket();
    vi.stubGlobal("window", {
      location: { protocol: "http:", host: "localhost" },
      ...(globalThis as Record<string, unknown>).window as object,
    });
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("delivers sessions_changed to the control handler, not onEvent", () => {
    const onEvent = vi.fn();
    const onControl = vi.fn();
    const client = new WebSocketClient("/ws", onEvent);
    client.setControlHandler(onControl);
    client.connect();
    const socket = created[0]!;

    receive(socket, { type: "sessions_changed", workspace: "/ws/a" });

    expect(onControl).toHaveBeenCalledTimes(1);
    expect(onControl).toHaveBeenCalledWith({
      type: "sessions_changed",
      workspace: "/ws/a",
    });
    expect(onEvent).not.toHaveBeenCalled();
  });

  it("falls back to onEvent passthrough when no control handler is set (legacy)", () => {
    const onEvent = vi.fn();
    const client = new WebSocketClient("/ws", onEvent);
    client.connect();
    const socket = created[0]!;

    receive(socket, { type: "sessions_changed", workspace: "/ws/a" });

    expect(onEvent).toHaveBeenCalledTimes(1);
  });

  it("unregisters the control handler on null", () => {
    const onEvent = vi.fn();
    const onControl = vi.fn();
    const client = new WebSocketClient("/ws", onEvent);
    client.setControlHandler(onControl);
    client.setControlHandler(null);
    client.connect();
    const socket = created[0]!;

    receive(socket, { type: "sessions_changed", workspace: "" });
    expect(onControl).not.toHaveBeenCalled();
    expect(onEvent).toHaveBeenCalledTimes(1);
  });

  it("still unwraps envelopes and forwards flat chat events unchanged", () => {
    const onEvent = vi.fn();
    const onControl = vi.fn();
    const client = new WebSocketClient("/ws", onEvent);
    client.setControlHandler(onControl);
    client.connect();
    const socket = created[0]!;

    receive(socket, {
      session_id: "abc.main",
      agent_name: "main",
      event_type: "user_message",
      pool: "main",
      parent_session_id: null,
      metadata: {},
      payload: { content: "hi" },
    });
    expect(onEvent).toHaveBeenCalledTimes(1);
    expect(onEvent.mock.calls[0]![0]).toMatchObject({
      event: "user_message",
      session_id: "abc.main",
      content: "hi",
    });
    expect(onControl).not.toHaveBeenCalled();
  });

  it("ignores a sessions_changed message with a non-string workspace", () => {
    const onEvent = vi.fn();
    const onControl = vi.fn();
    const client = new WebSocketClient("/ws", onEvent);
    client.setControlHandler(onControl);
    client.connect();
    const socket = created[0]!;

    receive(socket, { type: "sessions_changed", workspace: null });
    expect(onControl).not.toHaveBeenCalled();
  });
});
