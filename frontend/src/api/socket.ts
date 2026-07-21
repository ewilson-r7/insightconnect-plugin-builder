// WebSocket session channel wrapper (/ws/{session_id}).
//
// The backend pushes a `state` frame on connect, then a `turn`, `tokens`, and
// `visualization` frame after every applied message (icplugin_builder/api/app.py).
// This class abstracts connect/reconnect and frame parsing so the React layer
// only deals with typed callbacks.

import type { WsInboundFrame, WsSubmitMessageFrame } from "../types";

export type ConnectionStatus = "connecting" | "open" | "closed";

export interface SessionSocketHandlers {
  onFrame: (frame: WsInboundFrame) => void;
  onStatusChange?: (status: ConnectionStatus) => void;
}

/** Build the ws:// or wss:// URL for a session, honoring the page protocol. */
export function buildSocketUrl(sessionId: string, passphrase?: string | null): string {
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  const base = `${protocol}//${window.location.host}/ws/${encodeURIComponent(sessionId)}`;
  if (passphrase) {
    return `${base}?passphrase=${encodeURIComponent(passphrase)}`;
  }
  return base;
}

/**
 * A thin wrapper over a single session WebSocket.
 *
 * Frames are parsed from JSON and forwarded to `onFrame`; malformed frames are
 * ignored rather than crashing the UI. `submitMessage` sends the sole outbound
 * frame the backend accepts.
 */
export class SessionSocket {
  private socket: WebSocket | null = null;
  private readonly url: string;
  private readonly handlers: SessionSocketHandlers;

  constructor(sessionId: string, handlers: SessionSocketHandlers, passphrase?: string | null) {
    this.url = buildSocketUrl(sessionId, passphrase);
    this.handlers = handlers;
  }

  connect(): void {
    this.handlers.onStatusChange?.("connecting");
    const socket = new WebSocket(this.url);
    this.socket = socket;

    socket.onopen = () => this.handlers.onStatusChange?.("open");
    socket.onclose = () => this.handlers.onStatusChange?.("closed");
    socket.onmessage = (event: MessageEvent<string>) => {
      const frame = this.parseFrame(event.data);
      if (frame) {
        this.handlers.onFrame(frame);
      }
    };
  }

  private parseFrame(data: string): WsInboundFrame | null {
    try {
      const parsed = JSON.parse(data) as WsInboundFrame;
      if (parsed && typeof parsed.type === "string") {
        return parsed;
      }
    } catch {
      // Ignore non-JSON frames.
    }
    return null;
  }

  /** Send a message-submission frame; returns false if the socket is not open. */
  submitMessage(text: string): boolean {
    if (!this.socket || this.socket.readyState !== WebSocket.OPEN) {
      return false;
    }
    const frame: WsSubmitMessageFrame = { type: "submit_message", text };
    this.socket.send(JSON.stringify(frame));
    return true;
  }

  close(): void {
    this.socket?.close();
    this.socket = null;
  }
}
