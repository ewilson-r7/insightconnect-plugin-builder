import { describe, expect, it } from "vitest";
import { toFlowEdges, toFlowNodes } from "./layout";
import type { NodeView } from "../types";

function view(node_id: string, kind: NodeView["kind"], name: string): NodeView {
  return { node_id, kind, name, title: null, description: null, input: [], output: [] };
}

describe("layout", () => {
  it("maps every backend node to exactly one flow node carrying its view (Req 5.1)", () => {
    const views: NodeView[] = [
      view("connection", "connection", "connection"),
      view("action:a", "action", "a"),
      view("trigger:t", "trigger", "t"),
      view("task:k", "task", "k"),
    ];
    const nodes = toFlowNodes(views);
    expect(nodes).toHaveLength(4);
    expect(nodes.map((n) => n.id).sort()).toEqual(["action:a", "connection", "task:k", "trigger:t"]);
    expect(nodes.every((n) => n.type === "component")).toBe(true);
    expect(nodes.find((n) => n.id === "action:a")?.data.view.name).toBe("a");
  });

  it("stacks same-kind nodes in the same column at increasing rows", () => {
    const views: NodeView[] = [view("action:a", "action", "a"), view("action:b", "action", "b")];
    const [a, b] = toFlowNodes(views);
    expect(a.position.x).toEqual(b.position.x);
    expect(b.position.y).toBeGreaterThan(a.position.y);
  });

  it("links the connection to every action and trigger, but not tasks", () => {
    const views: NodeView[] = [
      view("connection", "connection", "connection"),
      view("action:a", "action", "a"),
      view("trigger:t", "trigger", "t"),
      view("task:k", "task", "k"),
    ];
    const edges = toFlowEdges(views);
    expect(edges.map((e) => e.target).sort()).toEqual(["action:a", "trigger:t"]);
    expect(edges.every((e) => e.source === "connection")).toBe(true);
  });

  it("produces no edges when there is no connection node", () => {
    expect(toFlowEdges([view("action:a", "action", "a")])).toEqual([]);
  });
});
