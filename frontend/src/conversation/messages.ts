// Chat transcript model and pure helpers for the Conversation_Interface.
//
// The backend keeps the authoritative draft but does not store a chat
// transcript, so the UI maintains its own message list. The functions here are
// pure so the transcript logic can be unit-tested without a live socket.

import type { TurnResult } from "../types";

/** Who authored a transcript entry. */
export type MessageRole = "user" | "system";

/** How a system message should be visually treated. */
export type MessageTone = "info" | "clarification" | "error";

export interface ChatMessage {
  id: string;
  role: MessageRole;
  tone: MessageTone;
  text: string;
}

let counter = 0;

/** Generate a stable, unique id for a transcript entry. */
export function nextMessageId(): string {
  counter += 1;
  return `m${counter}`;
}

/** Build a user transcript entry from raw input. */
export function userMessage(text: string): ChatMessage {
  return { id: nextMessageId(), role: "user", tone: "info", text };
}

/** Build a system transcript entry (info/clarification/error). */
export function systemMessage(text: string, tone: MessageTone = "info"): ChatMessage {
  return { id: nextMessageId(), role: "system", tone, text };
}

/**
 * Derive the system-facing transcript entry for a completed turn.
 *
 * - `clarification` turns surface the ambiguity prompt (Req 1.5, 22.5).
 * - `rejected_input` / `not_found` / `failed` surface the reason as an error
 *   and leave the draft unchanged (Req 1.6, 1.7, 15.4).
 * - `applied` turns confirm the draft update; when the turn produced no message
 *   a default confirmation is shown, reflecting that the draft state has been
 *   updated (Req 1.4).
 */
export function turnResultToMessage(result: TurnResult): ChatMessage {
  switch (result.status) {
    case "clarification":
      return systemMessage(
        result.message || "Your request was ambiguous. Please clarify.",
        "clarification",
      );
    case "rejected_input":
    case "not_found":
    case "failed":
      return systemMessage(
        result.message || "The request could not be completed.",
        "error",
      );
    case "applied":
    default:
      return systemMessage(result.message || "Draft updated.", "info");
  }
}
