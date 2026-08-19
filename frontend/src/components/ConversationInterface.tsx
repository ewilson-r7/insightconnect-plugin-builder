// Conversation_Interface (task 23.1).
//
// The chat panel of the local UI. It composes:
//   - the live cumulative token counter (Req 3.6),
//   - the private-source usage-restriction notice when forked from the private
//     production repository (Req 25.6),
//   - the scrolling message list including clarification prompts (Req 1.4, 1.5),
//   - the message input wired to the WebSocket session channel (Req 1.1, 1.6).
//
// Draft state, token counts, and clarification prompts arrive over the
// WebSocket via `useConversation`; entry-mode selection (Req 24.1) is handled
// by the parent before a session exists and passed in as `session`.

import { useEffect } from "react";

import { useConversation } from "../conversation/useConversation";
import type { SessionState, VisualizationPayload } from "../types";
import { MessageInput } from "./MessageInput";
import { MessageList } from "./MessageList";
import { PrivateSourceNotice } from "./PrivateSourceNotice";
import { TokenCounter } from "./TokenCounter";

export interface ConversationInterfaceProps {
  session: SessionState;
  passphrase?: string | null;
  /**
   * Lifts the live visualization payload -- folded from the shared session
   * socket this component owns -- up to the app shell so the Visualization_View
   * can render the same, per-turn-updated graph without opening a second socket
   * (task 24.1, Req 5.3).
   */
  onVisualization?: (payload: VisualizationPayload) => void;
}

export function ConversationInterface({
  session,
  passphrase,
  onVisualization,
}: ConversationInterfaceProps) {
  const { messages, tokenTotal, privateSourceNotice, connection, visualization, progress, submit } =
    useConversation({
      session,
      passphrase,
    });

  useEffect(() => {
    onVisualization?.(visualization);
  }, [visualization, onVisualization]);

  return (
    <section className="conversation" aria-label="Plugin builder conversation">
      <header className="conversation__header">
        <div className="conversation__identity">
          <span className="conversation__mode" data-testid="entry-mode">
            {entryModeLabel(session.entry_mode)}
          </span>
          {session.plugin_name && (
            <span className="conversation__plugin">{session.plugin_name}</span>
          )}
          <ConnectionBadge status={connection} />
        </div>
        <TokenCounter total={tokenTotal} />
      </header>

      <PrivateSourceNotice notice={privateSourceNotice} />

      <MessageList messages={messages} />

      <ProgressStatus phase={progress} />

      <MessageInput onSubmit={submit} disabled={connection !== "open"} />
    </section>
  );
}

/**
 * The phase currently running, in one region whose text is replaced (clause 2.19).
 *
 * `role="status"` is polite and, because a status region is atomic, each update
 * replaces the previous announcement rather than queueing behind it -- which is
 * what makes a per-second re-statement usable rather than a flood. Rendered
 * outside the transcript so a finished run leaves no trail of ticks behind it.
 */
function ProgressStatus({ phase }: { phase: string | null }) {
  return (
    <div
      className="conversation__progress"
      role="status"
      aria-atomic="true"
      data-testid="turn-progress"
    >
      {phase ?? ""}
    </div>
  );
}

function ConnectionBadge({ status }: { status: string }) {
  const label =
    status === "open" ? "Connected" : status === "connecting" ? "Connecting…" : "Disconnected";
  return (
    <span
      className={`conversation__connection conversation__connection--${status}`}
      role="status"
      data-testid="connection-badge"
    >
      {label}
    </span>
  );
}

/** Human-readable label for an entry mode identifier (Req 24.1). */
function entryModeLabel(mode: string): string {
  switch (mode) {
    case "create_new":
      return "New plugin";
    case "iterate_custom":
      return "Iterating custom plugin";
    case "enhance_production":
      return "Enhancing production plugin";
    default:
      return mode;
  }
}
