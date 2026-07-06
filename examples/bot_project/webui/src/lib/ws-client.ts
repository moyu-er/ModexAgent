import type { DeltaEnvelope, ServerEventUnion } from "../types/events";
import type { OutgoingAttachmentRef } from "../types/attachments";
import { unwrapEnvelope } from "../types/events";

type EventHandler = (event: ServerEventUnion) => void;
type CloseHandler = () => void;
type OpenHandler = () => void;

const WS_PATH = "/ws";

/** Reconnect policy for unexpected socket closures. */
const RECONNECT_BASE_DELAY_MS = 1000;
const RECONNECT_MAX_DELAY_MS = 30_000;
const RECONNECT_MAX_ATTEMPTS = 10;

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
  private readonly onOpen?: OpenHandler;
  private _connected: boolean = false;
  /** True when the current close is an explicit ``disconnect()`` — suppresses
   *  auto-reconnect. Reset to false on every fresh ``connect()``. */
  private _manualClose: boolean = false;
  /** Pending reconnect timer (set when a close schedules a retry). */
  private _reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  /** Consecutive reconnect attempts since the last successful open. */
  private _reconnectAttempts: number = 0;

  constructor(
    url: string,
    onEvent: EventHandler,
    onClose?: CloseHandler,
    onOpen?: OpenHandler,
  ) {
    this.url = url;
    this.onEvent = onEvent;
    this.onClose = onClose;
    this.onOpen = onOpen;
  }

  get connected(): boolean {
    return this._connected;
  }

  connect(): void {
    // A fresh manual connect cancels any pending reconnect and re-enables
    // the auto-reconnect policy for subsequent unexpected closes.
    this._manualClose = false;
    this._clearReconnectTimer();
    this._reconnectAttempts = 0;
    if (this.ws) {
      return;
    }
    this._openSocket();
  }

  private _openSocket(): void {
    const protocol = window.location.protocol === "https:" ? "wss" : "ws";
    this.ws = new WebSocket(`${protocol}://${window.location.host}${this.url}`);

    this.ws.onopen = (): void => {
      this._connected = true;
      // A successful open resets the backoff window — the next unexpected
      // close starts a fresh reconnect sequence.
      this._reconnectAttempts = 0;
      this.onOpen?.();
    };

    this.ws.onclose = (): void => {
      this._connected = false;
      this.ws = null;
      this.onClose?.();
      if (!this._manualClose) {
        this._scheduleReconnect();
      }
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

  /** Schedule a reconnect attempt with exponential backoff. Gives up after
   *  ``RECONNECT_MAX_ATTEMPTS`` tries so a permanently-down server does not
   *  spin forever. Each attempt's delay is ``base * 2^attempt`` capped at
   *  ``RECONNECT_MAX_DELAY_MS`` (1s, 2s, 4s, 8s, 16s, 30s, 30s, 30s, 30s, 30s). */
  private _scheduleReconnect(): void {
    if (this._reconnectAttempts >= RECONNECT_MAX_ATTEMPTS) {
      return;
    }
    const delay = Math.min(
      RECONNECT_BASE_DELAY_MS * 2 ** this._reconnectAttempts,
      RECONNECT_MAX_DELAY_MS,
    );
    this._reconnectAttempts += 1;
    this._reconnectTimer = setTimeout(() => {
      this._reconnectTimer = null;
      // A disconnect() during the delay flips _manualClose; respect it.
      if (this._manualClose) {
        return;
      }
      this._openSocket();
    }, delay);
  }

  private _clearReconnectTimer(): void {
    if (this._reconnectTimer !== null) {
      clearTimeout(this._reconnectTimer);
      this._reconnectTimer = null;
    }
  }

  disconnect(): void {
    this._manualClose = true;
    this._clearReconnectTimer();
    this._reconnectAttempts = 0;
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
  attach(sessionIdOrUuid: string, pool?: string, ws?: string): boolean {
    if (pool !== undefined) {
      return this.send("attach", {
        uuid_prefix: sessionIdOrUuid,
        pool,
        ...(ws ? { ws } : {}),
      });
    }
    return this.send("attach", { session_id: sessionIdOrUuid });
  }

  sendMessage(
    sessionId: string,
    content: string,
    ws?: string,
    requestId?: string,
    attachments?: OutgoingAttachmentRef[],
    providerName?: string,
    modelName?: string,
  ): boolean {
    return this.send("send_message", {
      session_id: sessionId,
      content,
      ...(ws ? { ws } : {}),
      // Carried so the backend can echo it back on the user_message event
      // (server.py reads data["_request_id"]) and the reducer can dedup the
      // optimistic message against the echo — without it every send renders
      // the user message twice (optimistic + un-deduped echo).
      ...(requestId ? { _request_id: requestId } : {}),
      // Uploaded-file refs ({local_path, filename?, mime?}) the backend builds
      // AttachmentRefs from so the ingest stage persists + perceives them.
      ...(attachments && attachments.length > 0 ? { attachments } : {}),
      // Model override: when the composer selects a non-default
      // (provider, model) pair, both fields ride the send_message payload so
      // the backend _ws_send_message routes the turn to the chosen model.
      ...(providerName ? { provider_name: providerName } : {}),
      ...(modelName ? { model_name: modelName } : {}),
    });
  }

  deleteConversation(sessionId: string): boolean {
    return this.send("delete_conversation", { session_id: sessionId });
  }

  /** Send a pause request for the currently streaming session. */
  pause(sessionId: string, ws?: string): boolean {
    return this.send("pause", {
      session_id: sessionId,
      ...(ws ? { ws } : {}),
    });
  }
}

export function buildWsUrl(): string {
  return WS_PATH;
}
