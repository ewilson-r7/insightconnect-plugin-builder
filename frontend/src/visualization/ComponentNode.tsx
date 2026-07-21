/**
 * Custom React Flow node rendering one plugin component.
 *
 * Each node shows its kind, name/title, and -- for actions and triggers -- the
 * input and output schema fields with their type and required/optional badge
 * (Req 5.2). The connection node shows only its input (connection) fields; task
 * nodes show their input fields. Handles are rendered so the connection node
 * can link to actions/triggers.
 */
import { Handle, Position, type Node, type NodeProps } from "@xyflow/react";
import type { FieldView } from "../types";
import type { FlowNodeData } from "./layout";

/** A compact list of schema fields with a required/optional badge (Req 5.2). */
function FieldList({ label, fields }: { label: string; fields: FieldView[] }) {
  return (
    <div className="node-schema" data-testid={`schema-${label.toLowerCase()}`}>
      <div className="node-schema__label">{label}</div>
      {fields.length === 0 ? (
        <div className="node-schema__empty">none</div>
      ) : (
        <ul className="node-schema__fields">
          {fields.map((field) => (
            <li key={field.name} className="node-field" data-testid="node-field">
              <span className="node-field__name">{field.name}</span>
              <span className="node-field__type">{field.type}</span>
              <span
                className={
                  field.required
                    ? "node-field__badge node-field__badge--required"
                    : "node-field__badge node-field__badge--optional"
                }
              >
                {field.required ? "required" : "optional"}
              </span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

export function ComponentNode({ data, selected }: NodeProps<Node<FlowNodeData>>) {
  const { view } = data;
  const isConnection = view.kind === "connection";
  const showOutput = view.kind === "action" || view.kind === "trigger";

  return (
    <div
      className={`component-node component-node--${view.kind}${selected ? " component-node--selected" : ""}`}
      data-testid={`node-${view.node_id}`}
    >
      <Handle type="target" position={Position.Left} />
      <div className="component-node__header">
        <span className="component-node__kind">{view.kind}</span>
        <span className="component-node__name">{view.title || view.name}</span>
      </div>
      <FieldList label={isConnection ? "Fields" : "Input"} fields={view.input} />
      {showOutput ? <FieldList label="Output" fields={view.output} /> : null}
      <Handle type="source" position={Position.Right} />
    </div>
  );
}
