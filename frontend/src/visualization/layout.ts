/**
 * Pure layout helpers that turn the backend visualization payload into the
 * nodes and edges React Flow renders.
 *
 * The graph groups nodes into four columns by kind -- connection, actions,
 * triggers, tasks -- so the structure of the plugin reads left-to-right at a
 * glance (Req 5.1). Edges connect the single connection node to every action
 * and trigger, reflecting that those components run against the connection.
 *
 * This module is deliberately free of React and React Flow rendering concerns
 * so the mapping (which every graph guarantee depends on) can be unit-tested in
 * isolation.
 */
import type { Edge, Node } from "@xyflow/react";
import type { NodeKind, NodeView } from "../types";

/** Data attached to each React Flow node; consumed by {@link ComponentNode}. */
export interface FlowNodeData extends Record<string, unknown> {
  view: NodeView;
}

/** Horizontal position (px) of each kind's column. */
const COLUMN_X: Record<NodeKind, number> = {
  connection: 0,
  action: 320,
  trigger: 640,
  task: 960,
};

/** Vertical spacing (px) between stacked nodes in a column. */
const ROW_GAP = 180;

/**
 * Build the React Flow nodes for a visualization payload's nodes.
 *
 * Every backend node becomes exactly one flow node carrying the original
 * {@link NodeView} as its data, so no component is dropped (Req 5.1) and each
 * node keeps its full input/output schema for display (Req 5.2).
 */
export function toFlowNodes(views: NodeView[]): Node<FlowNodeData>[] {
  const rowByColumn: Partial<Record<NodeKind, number>> = {};
  return views.map((view) => {
    const row = rowByColumn[view.kind] ?? 0;
    rowByColumn[view.kind] = row + 1;
    return {
      id: view.node_id,
      type: "component",
      position: { x: COLUMN_X[view.kind], y: row * ROW_GAP },
      data: { view },
    };
  });
}

/**
 * Build the edges linking the connection node to every action and trigger.
 *
 * When the payload has no connection node (an unusual draft), no edges are
 * produced. Tasks are not connection-bound and are left unconnected.
 */
export function toFlowEdges(views: NodeView[]): Edge[] {
  const connection = views.find((view) => view.kind === "connection");
  if (!connection) {
    return [];
  }
  return views
    .filter((view) => view.kind === "action" || view.kind === "trigger")
    .map((view) => ({
      id: `${connection.node_id}->${view.node_id}`,
      source: connection.node_id,
      target: view.node_id,
    }));
}
