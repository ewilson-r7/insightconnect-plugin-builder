import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import type {
  ConnectionStatus,
  SessionSocketHandlers,
} from "../api/socket";
import type { SessionState, WsInboundFrame } from "../types";

// The Conversation_Interface owns a live WebSocket session channel via
// `useConversation`. We replace `SessionSocket` with a controllable fake so the
// test can drive inbound frames (state/turn/tokens/error) and observe the
// resulting chat transcript, token counter, and notices without a real socket.

interface FakeSocketController {
  handlers: SessionSocketHandlers | null;
  submit: ReturnType<typeof vi.fn>;
  emit(frame: WsInboundFrame): void;
  setStatus(status: ConnectionStatus): void;
}

const controller: FakeSocketController = {
  handlers: null,
  submit: vi.fn(() => true),
  emit(frame) {
    act(() => {
      this.handlers?.onFrame(frame);
    });
  },
  setStatus(status) {
    act(() => {
      this.handlers?.onStatusChange?.(status);
    });
  },
};

vi.mock("../api/socket", () => {
  class FakeSessionSocket {
    private readonly handlers: SessionSocketHandlers;
    constructor(_sessionId: string, handlers: SessionSocketHandlers) {
      this.handlers = handlers;
      controller.handlers = handlers;
    }
    connect(): void {
      // Mirror an immediately-open connection so the input is enabled.
      this.handlers.onStatusChange?.("open");
    }
    submitMessage(text: string): boolean {
      return controller.submit(text) as boolean;
    }
    close(): void {}
  }
  return { SessionSocket: FakeSessionSocket };
});

// Imported after the mock is registered so the hook picks up the fake socket.
import { ConversationInterface } from "./ConversationInterface";

function makeSession(overrides: Partial<SessionState> = {}): SessionState {
  return {
    session_id: "sess-1",
    entry_mode: "create_new",
    plugin_name: null,
    private_source_notice: null,
    spec: null,
    token_total: 0,
    visualization: { state: "empty", message: null, error: null, nodes: [] },
    ...overrides,
  };
}

function stateFrame(session: SessionState): WsInboundFrame {
  return { type: "state", state: session };
}

beforeEach(() => {
  controller.handlers = null;
  controller.submit = vi.fn(() => true);
});

afterEach(() => {
  vi.clearAllMocks();
});

describe("ConversationInterface header (Req 24.1, 3.6)", () => {
  it("labels the active entry mode and shows the plugin name when present", () => {
    render(
      <ConversationInterface
        session={makeSession({ entry_mode: "enhance_production", plugin_name: "acme" })}
      />,
    );
    expect(screen.getByTestId("entry-mode")).toHaveTextContent("Enhancing production plugin");
    expect(screen.getByText("acme")).toBeInTheDocument();
  });

  it("starts the token counter from the session's cumulative total", () => {
    render(<ConversationInterface session={makeSession({ token_total: 250 })} />);
    expect(screen.getByTestId("token-total")).toHaveTextContent("250");
  });

  it("reflects the open connection status once connected", () => {
    render(<ConversationInterface session={makeSession()} />);
    // The fake socket opens on connect().
    expect(screen.getByTestId("connection-badge")).toHaveTextContent("Connected");
  });
});

describe("ConversationInterface token counter (Req 3.6)", () => {
  it("updates the cumulative token total from a tokens frame", async () => {
    render(<ConversationInterface session={makeSession({ token_total: 100 })} />);
    controller.emit({ type: "tokens", token_total: 4200 });
    await waitFor(() =>
      expect(screen.getByTestId("token-total")).toHaveTextContent((4200).toLocaleString()),
    );
  });

  it("updates the cumulative token total from an applied turn frame", async () => {
    render(<ConversationInterface session={makeSession()} />);
    controller.emit({
      type: "turn",
      result: {
        status: "applied",
        message: "Added action list_users",
        spec: null,
        generated: [],
        refreshed: false,
        structural_reasons: [],
        token_total: 1500,
      },
    });
    await waitFor(() =>
      expect(screen.getByTestId("token-total")).toHaveTextContent((1500).toLocaleString()),
    );
  });
});

describe("ConversationInterface private-source notice (Req 25.6)", () => {
  it("shows no notice for a net-new session", () => {
    render(<ConversationInterface session={makeSession()} />);
    expect(screen.queryByTestId("private-source-notice")).not.toBeInTheDocument();
  });

  it("surfaces the private-source notice delivered on the state frame", async () => {
    const session = makeSession({ entry_mode: "enhance_production" });
    render(<ConversationInterface session={makeSession()} />);
    controller.emit(
      stateFrame({
        ...session,
        private_source_notice: "Forked from the private production repo; internal use only.",
      }),
    );
    await waitFor(() =>
      expect(screen.getByTestId("private-source-notice")).toHaveTextContent(
        "internal use only",
      ),
    );
  });
});

describe("ConversationInterface transcript (Req 1.4, 1.5)", () => {
  it("renders a clarification prompt distinctly when a turn requests it", async () => {
    render(<ConversationInterface session={makeSession()} />);
    controller.emit({
      type: "turn",
      result: {
        status: "clarification",
        message: "Which action should I modify?",
        spec: null,
        generated: [],
        refreshed: false,
        structural_reasons: [],
        token_total: 0,
      },
    });
    const message = await screen.findByText("Which action should I modify?");
    const item = message.closest("li");
    expect(item).not.toBeNull();
    expect(item).toHaveAttribute("data-tone", "clarification");
    expect(within(item as HTMLElement).getByText("Clarification needed")).toBeInTheDocument();
  });

  it("renders an error frame as an error-toned system message", async () => {
    render(<ConversationInterface session={makeSession()} />);
    controller.emit({ type: "error", detail: "Unknown session" });
    const message = await screen.findByText("Unknown session");
    expect(message.closest("li")).toHaveAttribute("data-tone", "error");
  });
});

describe("ConversationInterface message submission (Req 1.1, 1.6)", () => {
  it("sends a valid message over the socket and echoes it into the transcript", async () => {
    const user = userEvent.setup();
    render(<ConversationInterface session={makeSession()} />);

    const field = screen.getByLabelText("Describe your plugin or the change you want");
    await user.type(field, "Add an action that lists users");
    await user.click(screen.getByTestId("send-button"));

    expect(controller.submit).toHaveBeenCalledWith("Add an action that lists users");
    const echoed = await screen.findByText("Add an action that lists users");
    expect(echoed.closest("li")).toHaveAttribute("data-role", "user");
    // The input clears after a successful submit.
    expect(field).toHaveValue("");
  });

  it("rejects whitespace-only input locally without sending it (Req 1.6)", async () => {
    const user = userEvent.setup();
    render(<ConversationInterface session={makeSession()} />);

    const field = screen.getByLabelText("Describe your plugin or the change you want");
    await user.type(field, "    ");
    // The send button stays disabled for whitespace-only input.
    expect(screen.getByTestId("send-button")).toBeDisabled();
    expect(controller.submit).not.toHaveBeenCalled();
  });

  it("disables input while the connection is not open", () => {
    render(<ConversationInterface session={makeSession()} />);
    // Simulate a dropped connection.
    controller.setStatus("closed");
    expect(screen.getByTestId("send-button")).toBeDisabled();
    expect(screen.getByTestId("connection-badge")).toHaveTextContent("Disconnected");
  });
});

describe("ConversationInterface turn progress (clause 2.19, task 14)", () => {
  it("re-states the running phase in one region instead of one entry per tick", async () => {
    render(<ConversationInterface session={makeSession()} />);
    act(() => controller.setStatus("open"));

    // A phase starting: an event, so it joins the transcript.
    act(() => controller.emit({ type: "status", message: "implementing the plugin" }));
    // Then the backend's ticker, once a second for as long as the run takes.
    for (const seconds of [1, 2, 3, 4, 5]) {
      act(() =>
        controller.emit({
          type: "status",
          message: `implementing the plugin (${seconds}s)`,
          progress: true,
        }),
      );
    }

    // The region shows the latest, and only the latest.
    const region = screen.getByTestId("turn-progress");
    expect(region).toHaveTextContent("implementing the plugin (5s)");
    expect(region).not.toHaveTextContent("(4s)");
    expect(region).toHaveAttribute("aria-atomic", "true");

    // The transcript gained the phase once, not once per tick. Before this the
    // ticks were appended to a polite live region, so a 13-minute run queued
    // roughly 780 near-identical announcements a screen reader could not skip.
    const items = screen.getAllByRole("listitem");
    const ticks = items.filter((item) => /\(\ds\)/.test(item.textContent ?? ""));
    expect(ticks).toHaveLength(0);
    expect(items.filter((i) => i.textContent === "implementing the plugin")).toHaveLength(1);
  });

  it("clears the phase when the turn ends, so a finished run does not read as running", async () => {
    render(<ConversationInterface session={makeSession()} />);
    act(() => controller.setStatus("open"));
    act(() =>
      controller.emit({ type: "status", message: "generating logic (3s)", progress: true }),
    );
    expect(screen.getByTestId("turn-progress")).toHaveTextContent("generating logic");

    act(() =>
      controller.emit({
        type: "turn",
        result: {
          status: "applied",
          message: "Added the action.",
          token_total: 10,
          spec: null,
        } as never,
      }),
    );

    expect(screen.getByTestId("turn-progress")).toHaveTextContent("");
  });
});
