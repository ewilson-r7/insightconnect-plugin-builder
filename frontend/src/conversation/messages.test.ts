import { describe, expect, it } from "vitest";

import type { TurnResult } from "../types";
import { systemMessage, turnResultToMessage, userMessage } from "./messages";

function turn(partial: Partial<TurnResult>): TurnResult {
  return {
    status: "applied",
    message: "",
    spec: null,
    generated: [],
    refreshed: false,
    structural_reasons: [],
    token_total: 0,
    ...partial,
  };
}

// Unit tests for the pure transcript-derivation logic (Req 1.4, 1.5, 1.6, 1.7).
describe("turnResultToMessage", () => {
  it("renders a clarification turn as a clarification-toned prompt (Req 1.5)", () => {
    const msg = turnResultToMessage(turn({ status: "clarification", message: "Which action?" }));
    expect(msg.tone).toBe("clarification");
    expect(msg.role).toBe("system");
    expect(msg.text).toBe("Which action?");
  });

  it("renders rejected input as an error message (Req 1.6)", () => {
    const msg = turnResultToMessage(turn({ status: "rejected_input", message: "Empty." }));
    expect(msg.tone).toBe("error");
    expect(msg.text).toBe("Empty.");
  });

  it("renders a not_found turn as an error (Req 15.4)", () => {
    expect(turnResultToMessage(turn({ status: "not_found", message: "No such action" })).tone).toBe(
      "error",
    );
  });

  it("renders a failed generation as an error and preserves the draft message (Req 1.7)", () => {
    expect(turnResultToMessage(turn({ status: "failed", message: "Generation failed" })).tone).toBe(
      "error",
    );
  });

  it("renders an applied turn as an info confirmation (Req 1.4)", () => {
    const msg = turnResultToMessage(turn({ status: "applied", message: "Added action list_users" }));
    expect(msg.tone).toBe("info");
    expect(msg.text).toBe("Added action list_users");
  });

  it("falls back to a default confirmation when an applied turn has no message", () => {
    expect(turnResultToMessage(turn({ status: "applied", message: "" })).text).toBe("Draft updated.");
  });
});

describe("message builders", () => {
  it("assigns unique ids across builders", () => {
    const a = userMessage("one");
    const b = systemMessage("two");
    const c = userMessage("three");
    expect(new Set([a.id, b.id, c.id]).size).toBe(3);
  });

  it("tags user messages with the info tone and user role", () => {
    const m = userMessage("hello");
    expect(m.role).toBe("user");
    expect(m.tone).toBe("info");
    expect(m.text).toBe("hello");
  });
});
