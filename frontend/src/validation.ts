// Client-side conversation-input validation, mirroring the backend gate
// (Req 1.1, 1.6). The backend re-validates authoritatively; this provides
// immediate feedback and prevents pointless round-trips.

/** Minimum accepted message length in characters (Req 1.1). */
export const MIN_MESSAGE_LENGTH = 1;

/** Maximum accepted message length in characters (Req 1.1). */
export const MAX_MESSAGE_LENGTH = 10_000;

/** Message shown when an empty/whitespace-only message is rejected (Req 1.6). */
export const EMPTY_INPUT_MESSAGE = "A non-empty description is required.";

/** Message shown when the message exceeds the maximum length (Req 1.1). */
export const TOO_LONG_MESSAGE = `Description must be at most ${MAX_MESSAGE_LENGTH.toLocaleString()} characters.`;

export interface ValidationResult {
  valid: boolean;
  reason?: string;
}

/**
 * Validate a raw conversation message before submission.
 *
 * Accepts iff the message has at least one non-whitespace character and its
 * length is within 1..10,000 (Req 1.1). Empty or whitespace-only input is
 * rejected without mutating the draft (Req 1.6). Length is measured on the raw
 * text so it matches the backend's character count.
 */
export function validateMessage(text: string): ValidationResult {
  if (text.trim().length === 0) {
    return { valid: false, reason: EMPTY_INPUT_MESSAGE };
  }
  if (text.length < MIN_MESSAGE_LENGTH || text.length > MAX_MESSAGE_LENGTH) {
    return { valid: false, reason: TOO_LONG_MESSAGE };
  }
  return { valid: true };
}
