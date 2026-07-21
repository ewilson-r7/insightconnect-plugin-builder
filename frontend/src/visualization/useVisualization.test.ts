import { describe, expect, it } from "vitest";
import { INITIAL_PAYLOAD, foldVisualizationFrame, payloadFromFrame } from "./useVisualization";
import type { VisualizationPayload, WsInboundFrame } from "../types";

const OK: VisualizationPayload = {
  state: "ok",
  message: null,
  error: null,
  nodes: [
    { node_id: "connection", kind: "connection", name: "connection", title: null, description: null, input: [], output: [] },
  ],
};

describe("visualization frame helpers", () => {
  it("extracts the payload from a state frame (initial connect)", () => {
    const frame: WsInboundFrame = {
      type: "state",
      state: {
        session_id: "s1",
        entry_mode: "create_new",
        plugin_name: null,
        private_source_notice: null,
        spec: null,
        token_total: 0,
        visualization: OK,
      },
    };
    expect(payloadFromFrame(frame)).toEqual(OK);
  });

  it("extracts the payload from a visualization frame (per-turn refresh)", () => {
    const frame: WsInboundFrame = { type: "visualization", visualization: OK };
    expect(payloadFromFrame(frame)).toEqual(OK);
  });

  it("returns null for frames that carry no visualization", () => {
    expect(payloadFromFrame({ type: "tokens", token_total: 5 })).toBeNull();
    expect(payloadFromFrame({ type: "error", detail: "boom" })).toBeNull();
  });

  it("folds a visualization frame in and leaves the payload unchanged otherwise", () => {
    expect(foldVisualizationFrame(INITIAL_PAYLOAD, { type: "visualization", visualization: OK })).toEqual(OK);
    const unchanged = foldVisualizationFrame(OK, { type: "tokens", token_total: 9 });
    expect(unchanged).toBe(OK);
  });
});
