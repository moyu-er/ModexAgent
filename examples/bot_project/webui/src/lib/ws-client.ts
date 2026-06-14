import type { DeltaEnvelope, ServerEventUnion } from "../types/events";
import { unwrapEnvelope } from "../types/events";

type EventHandler = (event: ServerEventUnion) => void;
type CloseHandler = () => void;

const WS_PATH = "/ws";

/** Check whether incoming JSON looks like a structured envelope. */
function isEnvelope(data: unknown): data is DeltaEnvelope {
  if (typeof data !== "object" || data === null) return false;
  const d = data as Record<string, unknown>;
  return typeof d["event_type"] === "string" &&
         typeof d["session_id"] === "string" &&
         typeof d["payload"] === "object";
}

export class WebSocketClient {
  private ws: WebSocket | null = null;
  private readonly url: string;
  private readonly onEvent: EventHandler;
  private readonly onClose?: CloseHandler;
  private _connected: boolean = false;

  constructor(url: string, onEvent: EventHandler, onClose?: CloseHandler) {
    this.url = url;
    this.onEvent = onEvent;
    this.onClose = onClose;
  }

  get connected(): boolean {
    return this._connected;
  }

  connect(): void {
    if (this.ws) {
      return;
    }
    const protocol = window.location.protocol === "https:" ? "wss" : "ws";
    this.ws = new WebSocket(`${protocol}://${window.location.host}${this.url}`);

    this.ws.onopen = (): void => {
      this._connected = true;
    };

    this.ws.onclose = (): void => {
      this._connected = false;
      this.ws = null;
      this.onClose?.();
    };

    this.ws.onerror = (): void => {
      this._connected = false;
    };

    this.ws.onmessage = (msg: MessageEvent<string>): void => {
      try {
        const data: unknown = JSON.parse(msg.data);
        // Structured envelope (new) — unwrap to flat event.
        if (isEnvelope(data)) {
          this.onEvent(unwrapEnvelope(data));
        } else {
          // Flat event (legacy path) — pass through.
          this.onEvent(data as unknown as ServerEventUnion);
        }
      } catch {
        // Ignore malformed messages
      }
    };
  }

  disconnect(): void {
    if (this.ws) {
      this.ws.close();
      this.ws = null;
      this._connected = false;
    }
  }

  /** Send a message. Returns true if sent, false if not connected. */
  send(type: string, payload: Record<string, unknown>): boolean {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) {
      console.warn(`WebSocket: cannot send "${type}" — not connected`);
      return false;
    }
    this.ws.send(JSON.stringify({ action: type, ...payload }));
    return true;
  }

  /**
   * Attach to a session.
   *
   * Two forms:
   *  - `attach(sessionId)`  → sends `{ session_id }` for an existing session.
   *  - `attach(uuidPrefix, pool)` → sends `{ uuid_prefix, pool }` to let the
   *    backend assemble the full session id.
   */
  attach(sessionIdOrUuid: string, pool?: string): boolean {
    if (pool !== undefined) {
      return this.send("attach", {
        uuid_prefix: sessionIdOrUuid,
        pool,
      });
    }
    return this.send("attach", { session_id: sessionIdOrUuid });
  }

  sendMessage(sessionId: string, content: string): boolean {
    return this.send("send_message", {
      session_id: sessionId,
      content,
    });
  }

  deleteConversation(sessionId: string): boolean {
    return this.send("delete_conversation", { session_id: sessionId });
  }
}

export function buildWsUrl(): string {
  return WS_PATH;
}
