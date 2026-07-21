/**
 * Visualization_View -- the live graphical representation of a plugin draft
 * (Req 5). Renders the connection, actions, triggers, and tasks as React Flow
 * nodes with their input/output schemas (Req 5.1, 5.2), lets the user select a
 * single component to see its detailed fields (Req 5.4), shows an empty-state
 * indication for a blank draft (Req 5.5), and on a parse failure shows an error
 * banner while retaining the most recently rendered valid graph (Req 5.6).
 *
 * The 2-second refresh guarantee (Req 5.3) is met by the backend pushing a new
 * payload over the WebSocket on every applied turn: this component simply
 * re-renders whenever its `payload` prop changes.
 */
import { useEffect, useMemo, useState } from "react";
import {
  Background,
  Controls,
  ReactFlow,
  ReactFlowProvider,
  useEdgesState,
  useNodesState,
  type Node,
  type NodeTypes,
  type OnSelectionChangeParams,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";

import type { NodeView, VisualizationPayload } from "../types";
import { ComponentNode } from "./ComponentNode";
import { DetailPanel } from "./DetailPanel";
import { toFlowEdges, toFlowNodes, type FlowNodeData } from "./layout";

const NODE_TYPES: NodeTypes = { component: ComponentNode };

export interface VisualizationViewProps {
  /** The latest visualization payload streamed from the backend. */
  payload: VisualizationPayload;
}

/**
 * Decide which nodes to render and whether a parse-error banner is shown,
 * retaining the last valid nodes across a parse failure (Req 5.6).
 */
function useRetainedNodes(payload: VisualizationPayload): {
  nodes: NodeView[];
  parseError: string | null;
} {
  const [lastValid, setLastValid] = useState<NodeView[]>(
    payload.state === "parse_error" ? [] : payload.nodes,
  );

  useEffect(() => {
    // Any non-parse-error render (ok or empty) is a valid visualization and
    // becomes the retained fallback for the next parse failure (Req 5.6).
    if (payload.state !== "parse_error") {
      setLastValid(payload.nodes);
    }
  }, [payload]);

  if (payload.state === "parse_error") {
    // Prefer nodes the backend retained; fall back to our own last-valid set.
    const retained = payload.nodes.length > 0 ? payload.nodes : lastValid;
    return { nodes: retained, parseError: payload.error ?? "The draft could not be parsed." };
  }
  return { nodes: payload.nodes, parseError: null };
}

function VisualizationGraph({ payload }: VisualizationViewProps) {
  const { nodes: viewNodes, parseError } = useRetainedNodes(payload);

  const flowNodes = useMemo(() => toFlowNodes(viewNodes), [viewNodes]);
  const flowEdges = useMemo(() => toFlowEdges(viewNodes), [viewNodes]);

  const [nodes, setNodes, onNodesChange] = useNodesState<Node<FlowNodeData>>(flowNodes);
  const [edges, setEdges, onEdgesChange] = useEdgesState(flowEdges);
  const [selected, setSelected] = useState<NodeView | null>(null);

  // Keep the rendered graph in sync with new payloads (the 2s refresh path).
  useEffect(() => {
    setNodes(flowNodes);
  }, [flowNodes, setNodes]);
  useEffect(() => {
    setEdges(flowEdges);
  }, [flowEdges, setEdges]);

  // Drop a stale selection when the selected node no longer exists in the draft.
  useEffect(() => {
    if (selected && !viewNodes.some((view) => view.node_id === selected.node_id)) {
      setSelected(null);
    }
  }, [viewNodes, selected]);

  const onSelectionChange = ({ nodes: selectedNodes }: OnSelectionChangeParams) => {
    // A single selected node opens the detail panel (Req 5.4); zero or multiple
    // selections close it.
    if (selectedNodes.length === 1) {
      const data = selectedNodes[0].data as FlowNodeData;
      setSelected(data.view);
    } else {
      setSelected(null);
    }
  };

  // Empty-state indication rather than a blank view (Req 5.5).
  const isEmpty = payload.state === "empty" || viewNodes.length === 0;

  return (
    <div className="visualization-view" data-testid="visualization-view">
      {parseError ? (
        <div className="visualization-view__error" role="alert" data-testid="parse-error">
          Draft could not be parsed: {parseError}. Showing the last valid visualization.
        </div>
      ) : null}

      <div className="visualization-view__graph">
        {isEmpty && !parseError ? (
          <div className="visualization-view__empty" data-testid="empty-state">
            {payload.message ?? "No plugin components yet."}
          </div>
        ) : (
          <ReactFlow
            nodes={nodes}
            edges={edges}
            nodeTypes={NODE_TYPES}
            onNodesChange={onNodesChange}
            onEdgesChange={onEdgesChange}
            onSelectionChange={onSelectionChange}
            fitView
          >
            <Background />
            <Controls />
          </ReactFlow>
        )}
      </div>

      <DetailPanel node={selected} />
    </div>
  );
}

/** Public entry point, wrapped in the React Flow provider for hook support. */
export function VisualizationView({ payload }: VisualizationViewProps) {
  return (
    <ReactFlowProvider>
      <VisualizationGraph payload={payload} />
    </ReactFlowProvider>
  );
}
