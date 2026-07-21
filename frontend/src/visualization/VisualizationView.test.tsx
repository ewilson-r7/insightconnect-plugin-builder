import { describe, expect, it } from "vitest";
import { render, screen, within } from "@testing-library/react";
import { VisualizationView } from "./VisualizationView";
import { DetailPanel } from "./DetailPanel";
import type { FieldView, NodeView, VisualizationPayload } from "../types";

function field(name: string, type: string, required: boolean): FieldView {
  return { name, type, required, title: null, description: `${name} description` };
}

function node(
  node_id: string,
  kind: NodeView["kind"],
  name: string,
  input: FieldView[] = [],
  output: FieldView[] = [],
): NodeView {
  return { node_id, kind, name, title: null, description: null, input, output };
}

function okPayload(nodes: NodeView[]): VisualizationPayload {
  return { state: "ok", message: null, error: null, nodes };
}

const SAMPLE: NodeView[] = [
  node("connection", "connection", "connection", [field("api_key", "password", true)]),
  node(
    "action:list_things",
    "action",
    "list_things",
    [field("query", "string", false)],
    [field("things", "[]string", true)],
  ),
  node("trigger:on_thing", "trigger", "on_thing", [], [field("thing", "string", true)]),
  node("task:sweep", "task", "sweep", [field("since", "date", false)]),
];

describe("VisualizationView", () => {
  it("renders a node for the connection, actions, triggers, and tasks (Req 5.1)", () => {
    render(<VisualizationView payload={okPayload(SAMPLE)} />);
    expect(screen.getByTestId("node-connection")).toBeInTheDocument();
    expect(screen.getByTestId("node-action:list_things")).toBeInTheDocument();
    expect(screen.getByTestId("node-trigger:on_thing")).toBeInTheDocument();
    expect(screen.getByTestId("node-task:sweep")).toBeInTheDocument();
  });

  it("shows input and output schema fields for an action (Req 5.2)", () => {
    render(<VisualizationView payload={okPayload(SAMPLE)} />);
    const action = screen.getByTestId("node-action:list_things");
    const input = within(action).getByTestId("schema-input");
    const output = within(action).getByTestId("schema-output");
    expect(within(input).getByText("query")).toBeInTheDocument();
    expect(within(input).getByText("optional")).toBeInTheDocument();
    expect(within(output).getByText("things")).toBeInTheDocument();
    expect(within(output).getByText("required")).toBeInTheDocument();
  });

  it("renders no detail panel until a single node is selected (Req 5.4)", () => {
    render(<VisualizationView payload={okPayload(SAMPLE)} />);
    expect(screen.queryByTestId("detail-panel")).not.toBeInTheDocument();
  });

  it("shows exactly the selected component's input and output fields (Req 5.4)", () => {
    // The detail panel is the Visualization_View's single-selection surface; it
    // renders exactly the selected node's fields. React Flow's click-driven
    // selection wires this node into the panel at runtime.
    const action = SAMPLE.find((n) => n.node_id === "action:list_things")!;
    render(<DetailPanel node={action} />);
    const panel = screen.getByTestId("detail-panel");
    expect(within(panel).getByText("list_things")).toBeInTheDocument();
    expect(within(panel).getByText("query")).toBeInTheDocument();
    expect(within(panel).getByText("things")).toBeInTheDocument();
    // Only this component's fields appear -- nothing from other nodes.
    expect(within(panel).queryByText("api_key")).not.toBeInTheDocument();
    expect(within(panel).getAllByTestId("detail-field")).toHaveLength(2);
  });

  it("renders nothing when no single node is selected (Req 5.4)", () => {
    const { container } = render(<DetailPanel node={null} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("shows an empty-state indication for an empty draft, not a blank view (Req 5.5)", () => {
    const payload: VisualizationPayload = {
      state: "empty",
      message: "No plugin components yet.",
      error: null,
      nodes: [],
    };
    render(<VisualizationView payload={payload} />);
    expect(screen.getByTestId("empty-state")).toHaveTextContent("No plugin components yet.");
  });

  it("retains the last valid graph and shows an error banner on parse failure (Req 5.6)", () => {
    const { rerender } = render(<VisualizationView payload={okPayload(SAMPLE)} />);
    expect(screen.getByTestId("node-action:list_things")).toBeInTheDocument();

    const parseError: VisualizationPayload = {
      state: "parse_error",
      message: null,
      error: "mapping values are not allowed here (line 3)",
      nodes: [],
    };
    rerender(<VisualizationView payload={parseError} />);

    expect(screen.getByTestId("parse-error")).toHaveTextContent("could not be parsed");
    // The last valid visualization is still shown.
    expect(screen.getByTestId("node-action:list_things")).toBeInTheDocument();
  });
});
