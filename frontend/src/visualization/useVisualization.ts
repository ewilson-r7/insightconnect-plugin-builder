/**
 * Frame-folding helpers for the Visualization_View (Req 5.3).
 *
 * The Visualization_View is prop-driven: it renders whatever
 * `VisualizationPayload` it is handed. The backend streams that payload over the
 * shared session WebSocket -- a `state` frame on connect (carrying the initial
 * visualization) and a `visualization` frame after every applied turn
 * (`icplugin_builder/api/app.py`). Because those frames arrive on the same
 * socket the conversation uses, the app shell (task 24.1) folds them into a
 * payload with {@link foldVisualizationFrame} and passes the result down, rather
 * than opening a second socket that would miss per-turn updates.
 */
import type { VisualizationPayload, WsInboundFrame } from "../types";

/** The empty-state payload shown before the first frame arrives (Req 5.5). */
export const INITIAL_PAYLOAD: VisualizationPayload = {
  state: "empty",
  message: "No plugin components yet.",
  error: null,
  nodes: [],
};

/**
 * Extract a visualization payload from a session-channel frame, if it carries
 * one. `state` frames (sent on connect) and `visualization` frames (sent after
 * each applied turn) both carry a payload; all other frames return `null`.
 */
export function payloadFromFrame(frame: WsInboundFrame): VisualizationPayload | null {
  if (frame.type === "visualization") {
    return frame.visualization;
  }
  if (frame.type === "state") {
    return frame.state.visualization;
  }
  return null;
}

/**
 * Fold an inbound frame into the current visualization payload.
 *
 * Returns the frame's payload when it carries one (satisfying the 2-second
 * refresh path in Req 5.3), otherwise leaves the current payload unchanged so
 * non-visualization frames (turn/tokens/error) don't disturb the graph.
 */
export function foldVisualizationFrame(
  current: VisualizationPayload,
  frame: WsInboundFrame,
): VisualizationPayload {
  return payloadFromFrame(frame) ?? current;
}
