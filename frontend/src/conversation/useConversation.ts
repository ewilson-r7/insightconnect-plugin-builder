// React hook wiring the Conversation_Interface to the backend session channel.
//
// It owns the chat transcript, the live cumulative token counter (Req 3.6), the
// private-source usage notice (Req 25.6), and the WebSocket lifecycle. Message
// submission is validated client-side (Req 1.1, 1.6) before being sent over the
// socket, and each turn result is folded into the transcript (Req 1.4, 1.5).

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { SessionSocket, type ConnectionStatus } from "../api/socket";
import type { MessageAttachment, SessionState, VisualizationPayload, WsInboundFrame } from "../types";
import { foldVisualizationFrame } from "../visualization/useVisualization";
import { validateMessage } from "../validation";
import {
  type ChatMessage,
  systemMessage,
  turnResultToMessage,
  userMessage,
} from "./messages";

export interface UseConversationOptions {
  session: SessionState;
  passphrase?: string | null;
}

export interface UseConversationResult {
  messages: ChatMessage[];
  tokenTotal: number;
  privateSourceNotice: string | null;
  connection: ConnectionStatus;
  /**
   * The live visualization view-model, folded from the same session socket the
   * conversation uses (Req 5.3). Sharing one socket keeps the graph in step
   * with every applied turn; a second socket would never see per-turn frames.
   */
  visualization: VisualizationPayload;
  /** Submit a message; returns the client-side validation result. */
  submit: (text: string) => { accepted: boolean; reason?: string };
}

export function useConversation({
  session,
  passphrase,
}: UseConversationOptions): UseConversationResult {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [tokenTotal, setTokenTotal] = useState<number>(session.token_total);
  const [privateSourceNotice, setPrivateSourceNotice] = useState<string | null>(
    session.private_source_notice,
  );
  const [connection, setConnection] = useState<ConnectionStatus>("connecting");
  const [visualization, setVisualization] = useState<VisualizationPayload>(
    session.visualization,
  );
  const socketRef = useRef<SessionSocket | null>(null);

  const handleFrame = useCallback((frame: WsInboundFrame) => {
    // Fold the visualization payload from the state/visualization frames on this
    // shared socket; other frame kinds leave the current graph unchanged (Req 5.3).
    setVisualization((prev) => foldVisualizationFrame(prev, frame));
    switch (frame.type) {
      case "state":
        setTokenTotal(frame.state.token_total);
        setPrivateSourceNotice(frame.state.private_source_notice);
        break;
      case "turn":
        setMessages((prev) => [...prev, turnResultToMessage(frame.result)]);
        setTokenTotal(frame.result.token_total);
        break;
      case "tokens":
        setTokenTotal(frame.token_total);
        break;
      case "error":
        setMessages((prev) => [...prev, systemMessage(frame.detail, "error")]);
        break;
      case "status":
        setMessages((prev) => [...prev, systemMessage(frame.message, "info")]);
        break;
      case "visualization":
        // The payload was already folded above; the Visualization_View
        // (task 23.2) renders it, so the chat has nothing more to do here.
        break;
    }
  }, []);

  useEffect(() => {
    const socket = new SessionSocket(
      session.session_id,
      { onFrame: handleFrame, onStatusChange: setConnection },
      passphrase,
    );
    socketRef.current = socket;
    socket.connect();
    return () => {
      socket.close();
      socketRef.current = null;
    };
  }, [session.session_id, passphrase, handleFrame]);

  const submit = useCallback(
    (text: string, attachments?: MessageAttachment[]): { accepted: boolean; reason?: string } => {
      const validation = validateMessage(text);
      if (!validation.valid && (!attachments || attachments.length === 0)) {
        // Reject locally without mutating the draft (Req 1.6).
        setMessages((prev) => [
          ...prev,
          systemMessage(validation.reason ?? "Invalid input.", "error"),
        ]);
        return { accepted: false, reason: validation.reason };
      }
      const sent = socketRef.current?.submitMessage(text, attachments) ?? false;
      if (!sent) {
        setMessages((prev) => [
          ...prev,
          systemMessage("Not connected. Please wait for the connection to open.", "error"),
        ]);
        return { accepted: false, reason: "not connected" };
      }
      const displayText = attachments?.length
        ? `${text}\n[Attached: ${attachments.map((a) => a.name).join(", ")}]`
        : text;
      setMessages((prev) => [...prev, userMessage(displayText)]);
      return { accepted: true };
    },
    [],
  );

  return useMemo(
    () => ({ messages, tokenTotal, privateSourceNotice, connection, visualization, submit }),
    [messages, tokenTotal, privateSourceNotice, connection, visualization, submit],
  );
}
