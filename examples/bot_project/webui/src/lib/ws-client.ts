import type { ServerEventUnion } from "../types/events";

type EventHandler = (event: ServerEventUnion) => void;

const WS_PATH = "/ws";

export class WebSocketClient {
  private ws: WebSocket | null = null;
  private readonly url: string;
  private readonly onEvent: EventHandler;
  private _connected: boolean = false;

  constructor(url: string, onEvent: EventHandler) {
    this.url = url;
    this.onEvent = onEvent;
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
    };

    this.ws.onerror = (): void => {
      this._connected = false;
    };

    this.ws.onmessage = (msg: MessageEvent<string>): void => {
      try {
        const data = JSON.parse(msg.data) as ServerEventUnion;
        this.onEvent(data);
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

  attach(conversationId: string): boolean {
    return this.send("attach", { conversation_id: conversationId });
  }

  sendMessage(conversationId: string, content: string): boolean {
    return this.send("send_message", {
      conversation_id: conversationId,
      content,
    });
  }

  newConversation(): boolean {
    return this.send("new_conversation", {});
  }

  deleteConversation(conversationId: string): boolean {
    return this.send("delete_conversation", { conversation_id: conversationId });
  }
}

export function buildWsUrl(): string {
  return WS_PATH;
}
