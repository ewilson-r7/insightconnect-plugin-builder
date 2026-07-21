import { describe, expect, it } from "vitest";

import {
  EMPTY_INPUT_MESSAGE,
  MAX_MESSAGE_LENGTH,
  TOO_LONG_MESSAGE,
  validateMessage,
} from "./validation";

// Unit tests for the client-side conversation-input gate (Req 1.1, 1.6).
describe("validateMessage", () => {
  it("rejects an empty string with the non-empty message (Req 1.6)", () => {
    expect(validateMessage("")).toEqual({ valid: false, reason: EMPTY_INPUT_MESSAGE });
  });

  it("rejects whitespace-only input (Req 1.6)", () => {
    expect(validateMessage("   \n\t ")).toEqual({ valid: false, reason: EMPTY_INPUT_MESSAGE });
  });

  it("accepts a single non-whitespace character (lower boundary, Req 1.1)", () => {
    expect(validateMessage("a")).toEqual({ valid: true });
  });

  it("accepts input at the maximum length (upper boundary, Req 1.1)", () => {
    expect(validateMessage("x".repeat(MAX_MESSAGE_LENGTH))).toEqual({ valid: true });
  });

  it("rejects input one character over the maximum (Req 1.1)", () => {
    expect(validateMessage("x".repeat(MAX_MESSAGE_LENGTH + 1))).toEqual({
      valid: false,
      reason: TOO_LONG_MESSAGE,
    });
  });

  it("measures raw length so surrounding whitespace counts toward the cap", () => {
    // A single visible char padded to exactly the max is still accepted...
    const padded = ` ${"x".repeat(MAX_MESSAGE_LENGTH - 2)} `;
    expect(padded.length).toBe(MAX_MESSAGE_LENGTH);
    expect(validateMessage(padded)).toEqual({ valid: true });
    // ...but one more character of padding pushes it over.
    expect(validateMessage(padded + " ")).toEqual({ valid: false, reason: TOO_LONG_MESSAGE });
  });
});
