import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { DiffView } from "./DiffView";
import type { FileTreeDiff } from "../../types";

describe("DiffView", () => {
  it("indicates the first version and presents all files as additions (Req 16.4)", () => {
    const diff: FileTreeDiff = {
      added: ["plugin.spec.yaml", "help.md", "Dockerfile"],
      removed: [],
      modified: [],
      first_version: true,
    };
    render(<DiffView diff={diff} />);

    expect(screen.getByTestId("diff-first-version")).toBeInTheDocument();
    const added = screen.getByTestId("diff-added");
    expect(added).toHaveTextContent("Added (3)");
    // No removed/modified groups render for a first version.
    expect(screen.queryByTestId("diff-removed")).not.toBeInTheDocument();
    expect(screen.queryByTestId("diff-modified")).not.toBeInTheDocument();
  });

  it("identifies added, removed, and modified files against a prior version (Req 16.3)", () => {
    const diff: FileTreeDiff = {
      added: ["new_action.py"],
      removed: ["old_action.py"],
      modified: ["plugin.spec.yaml"],
      first_version: false,
    };
    render(<DiffView diff={diff} />);

    expect(screen.queryByTestId("diff-first-version")).not.toBeInTheDocument();
    expect(screen.getByTestId("diff-added")).toHaveTextContent("new_action.py");
    expect(screen.getByTestId("diff-removed")).toHaveTextContent("old_action.py");
    expect(screen.getByTestId("diff-modified")).toHaveTextContent("plugin.spec.yaml");
  });

  it("reports no changes when the diff is empty against a prior version", () => {
    const diff: FileTreeDiff = {
      added: [],
      removed: [],
      modified: [],
      first_version: false,
    };
    render(<DiffView diff={diff} />);

    expect(screen.getByTestId("diff-no-changes")).toBeInTheDocument();
  });
});
