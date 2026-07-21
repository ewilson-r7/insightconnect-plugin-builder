/**
 * Single-selection detail panel (Req 5.4).
 *
 * When exactly one component node is selected in the graph, this panel shows
 * that component's detailed fields -- input followed by output -- with each
 * field's name, type, and required/optional status. It renders nothing when no
 * single node is selected, so the graph is the primary view until the user
 * drills into a component.
 */
import type { FieldView, NodeView } from "../types";

function DetailFieldRows({ fields }: { fields: FieldView[] }) {
  return (
    <>
      {fields.map((field) => (
        <tr key={field.name} data-testid="detail-field">
          <td className="detail-field__name">{field.name}</td>
          <td className="detail-field__type">{field.type}</td>
          <td className="detail-field__required">{field.required ? "required" : "optional"}</td>
          <td className="detail-field__description">{field.description ?? ""}</td>
        </tr>
      ))}
    </>
  );
}

export function DetailPanel({ node }: { node: NodeView | null }) {
  if (node === null) {
    return null;
  }

  const showOutput = node.kind === "action" || node.kind === "trigger";
  const hasInput = node.input.length > 0;
  const hasOutput = showOutput && node.output.length > 0;

  return (
    <aside className="detail-panel" data-testid="detail-panel" aria-label="Selected component detail">
      <header className="detail-panel__header">
        <span className="detail-panel__kind">{node.kind}</span>
        <h2 className="detail-panel__title">{node.title || node.name}</h2>
      </header>
      {node.description ? <p className="detail-panel__description">{node.description}</p> : null}

      <section className="detail-panel__section">
        <h3>{node.kind === "connection" ? "Connection fields" : "Input"}</h3>
        {hasInput ? (
          <table className="detail-table">
            <thead>
              <tr>
                <th>Name</th>
                <th>Type</th>
                <th>Required</th>
                <th>Description</th>
              </tr>
            </thead>
            <tbody>
              <DetailFieldRows fields={node.input} />
            </tbody>
          </table>
        ) : (
          <p className="detail-panel__empty">No input fields.</p>
        )}
      </section>

      {showOutput ? (
        <section className="detail-panel__section">
          <h3>Output</h3>
          {hasOutput ? (
            <table className="detail-table">
              <thead>
                <tr>
                  <th>Name</th>
                  <th>Type</th>
                  <th>Required</th>
                  <th>Description</th>
                </tr>
              </thead>
              <tbody>
                <DetailFieldRows fields={node.output} />
              </tbody>
            </table>
          ) : (
            <p className="detail-panel__empty">No output fields.</p>
          )}
        </section>
      ) : null}
    </aside>
  );
}
