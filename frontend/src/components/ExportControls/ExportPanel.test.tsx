import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { ExportPanel, type ExportClient } from "./ExportPanel";
import type { ExportOutcome, ExportPlan } from "../../types";

function makePlan(overrides: Partial<ExportPlan> = {}): ExportPlan {
  return {
    permitted: true,
    summary: "Ready to export",
    spec_preview: { name: "acme", vendor: "rapid7_custom", version: "1.0.0" },
    file_list: ["plugin.spec.yaml", "help.md"],
    diff: { added: ["plugin.spec.yaml", "help.md"], removed: [], modified: [], first_version: true },
    version_display: "1.0.0 -> 1.0.1",
    spec_errors: [],
    failed_stages: [],
    ...overrides,
  };
}

function makeClient(plan: ExportPlan, outcome?: ExportOutcome): ExportClient {
  return {
    prepareExport: vi.fn().mockResolvedValue(plan),
    confirmExport: vi.fn().mockResolvedValue(
      outcome ?? {
        status: "succeeded",
        message: "Exported",
        artifact_path: "/tmp/acme-1.0.1.plg",
        version: "1.0.1",
        target: "local",
        failure: null,
        retained_artifact_path: null,
      },
    ),
  };
}

describe("ExportPanel preview/diff/confirm (Req 16)", () => {
  it("shows the spec preview, file list, and diff after preparing (Req 16.1-16.4)", async () => {
    const user = userEvent.setup();
    const client = makeClient(makePlan());
    render(<ExportPanel sessionId="s1" client={client} />);

    await user.click(screen.getByTestId("prepare-export"));

    await waitFor(() => expect(screen.getByTestId("export-preview")).toBeInTheDocument());
    expect(client.prepareExport).toHaveBeenCalledWith("s1", undefined);
    expect(screen.getByTestId("spec-preview-body")).toHaveTextContent("rapid7_custom");
    expect(screen.getByTestId("version-display")).toHaveTextContent("1.0.0 -> 1.0.1");
    expect(screen.getByTestId("file-list")).toHaveTextContent("plugin.spec.yaml");
    expect(screen.getByTestId("diff-first-version")).toBeInTheDocument();
  });

  it("requires explicit confirmation before enabling export (Req 16.5)", async () => {
    const user = userEvent.setup();
    const client = makeClient(makePlan());
    render(<ExportPanel sessionId="s1" client={client} />);

    await user.click(screen.getByTestId("prepare-export"));
    await waitFor(() => expect(screen.getByTestId("confirm-controls")).toBeInTheDocument());

    // Export is disabled until the operator confirms the preview.
    expect(screen.getByTestId("confirm-export")).toBeDisabled();

    await user.click(screen.getByTestId("confirm-checkbox"));
    expect(screen.getByTestId("confirm-export")).toBeEnabled();

    await user.click(screen.getByTestId("confirm-export"));
    await waitFor(() => expect(screen.getByTestId("export-success")).toBeInTheDocument());
    expect(client.confirmExport).toHaveBeenCalledTimes(1);
    expect(client.confirmExport).toHaveBeenCalledWith(
      "s1",
      expect.objectContaining({ confirmed: true, target: "local" }),
      undefined,
    );
  });

  it("aborts with no export when the operator cancels (Req 16.6)", async () => {
    const user = userEvent.setup();
    const client = makeClient(makePlan());
    render(<ExportPanel sessionId="s1" client={client} />);

    await user.click(screen.getByTestId("prepare-export"));
    await waitFor(() => expect(screen.getByTestId("confirm-controls")).toBeInTheDocument());
    await user.click(screen.getByTestId("confirm-checkbox"));

    await user.click(screen.getByTestId("cancel-export"));

    // No confirm request is sent (no artifact produced) and the preview clears.
    expect(client.confirmExport).not.toHaveBeenCalled();
    expect(screen.queryByTestId("export-preview")).not.toBeInTheDocument();
  });

  it("blocks export and lists validation errors when the gate denies (Req 7.4, 8.6)", async () => {
    const user = userEvent.setup();
    const client = makeClient(
      makePlan({
        permitted: false,
        summary: "Spec invalid",
        spec_errors: [{ path: "connection.host", message: "required" }],
        failed_stages: ["lint", "test"],
      }),
    );
    render(<ExportPanel sessionId="s1" client={client} />);

    await user.click(screen.getByTestId("prepare-export"));
    await waitFor(() => expect(screen.getByTestId("export-blocked")).toBeInTheDocument());

    expect(screen.queryByTestId("confirm-controls")).not.toBeInTheDocument();
    expect(screen.getByTestId("blocked-spec-errors")).toHaveTextContent("connection.host");
    expect(screen.getByTestId("blocked-failed-stages")).toHaveTextContent("lint");
  });

  it("requires tenant credentials to be entered for a tenant export", async () => {
    const user = userEvent.setup();
    const client = makeClient(makePlan());
    render(<ExportPanel sessionId="s1" client={client} />);

    await user.click(screen.getByTestId("prepare-export"));
    await waitFor(() => expect(screen.getByTestId("confirm-controls")).toBeInTheDocument());

    await user.click(screen.getByTestId("target-tenant"));
    expect(screen.getByTestId("tenant-credentials")).toBeInTheDocument();
    await user.type(screen.getByTestId("region-base-url"), "https://us.api.insight.rapid7.com");
    await user.type(screen.getByTestId("api-key"), "secret-key");
    await user.click(screen.getByTestId("confirm-checkbox"));
    await user.click(screen.getByTestId("confirm-export"));

    await waitFor(() =>
      expect(client.confirmExport).toHaveBeenCalledWith(
        "s1",
        expect.objectContaining({
          target: "tenant",
          region_base_url: "https://us.api.insight.rapid7.com",
          api_key: "secret-key",
        }),
        undefined,
      ),
    );
  });
});

describe("ExportPanel build/export failure display (Req 19)", () => {
  it("distinguishes a build failure and shows the failing step output (Req 19.1, 19.4)", async () => {
    const user = userEvent.setup();
    const outcome: ExportOutcome = {
      status: "build_failed",
      message: "Build failed at lint",
      artifact_path: null,
      version: null,
      target: null,
      failure: {
        kind: "build",
        failing_step: "lint",
        displayed_output: "E501 line too long",
        full_output: "E501 line too long",
        truncated: false,
      },
      retained_artifact_path: null,
    };
    const client = makeClient(makePlan(), outcome);
    render(<ExportPanel sessionId="s1" client={client} />);

    await user.click(screen.getByTestId("prepare-export"));
    await waitFor(() => expect(screen.getByTestId("confirm-controls")).toBeInTheDocument());
    await user.click(screen.getByTestId("confirm-checkbox"));
    await user.click(screen.getByTestId("confirm-export"));

    await waitFor(() => expect(screen.getByTestId("failure-indicator")).toBeInTheDocument());
    const indicator = screen.getByTestId("failure-indicator");
    expect(indicator).toHaveAttribute("data-failure-kind", "build");
    expect(screen.getByTestId("failure-title")).toHaveTextContent("Build failed");
    expect(screen.getByTestId("failure-step")).toHaveTextContent("lint");
    expect(screen.getByTestId("error-output-body")).toHaveTextContent("E501 line too long");
  });

  it("distinguishes an export failure, truncates output, and surfaces retention (Req 19.2, 19.4, 19.5)", async () => {
    const user = userEvent.setup();
    const full = "connection refused\n".repeat(1000); // > 10,000 chars
    const outcome: ExportOutcome = {
      status: "export_failed",
      message: "Tenant upload failed",
      artifact_path: null,
      version: "1.0.1",
      target: "https://us.api.insight.rapid7.com",
      failure: {
        kind: "export",
        failing_step: "tenant upload",
        displayed_output: full.slice(0, 10_000),
        full_output: full,
        truncated: true,
      },
      retained_artifact_path: "/tmp/acme-1.0.1.plg",
    };
    const client = makeClient(makePlan(), outcome);
    render(<ExportPanel sessionId="s1" client={client} />);

    await user.click(screen.getByTestId("prepare-export"));
    await waitFor(() => expect(screen.getByTestId("confirm-controls")).toBeInTheDocument());
    await user.click(screen.getByTestId("confirm-checkbox"));
    await user.click(screen.getByTestId("confirm-export"));

    await waitFor(() => expect(screen.getByTestId("failure-indicator")).toBeInTheDocument());
    expect(screen.getByTestId("failure-indicator")).toHaveAttribute(
      "data-failure-kind",
      "export",
    );
    expect(screen.getByTestId("failure-step")).toHaveTextContent("tenant upload");
    expect(screen.getByTestId("error-output-body").textContent).toHaveLength(10_000);
    expect(screen.getByTestId("failure-retained-artifact")).toHaveTextContent(
      "/tmp/acme-1.0.1.plg",
    );

    // Full output is retained and reachable (Req 19.5).
    await user.click(screen.getByTestId("error-output-show-full"));
    expect(screen.getByTestId("error-output-body").textContent).toHaveLength(full.length);
  });

  it("surfaces a request error if prepare fails without exporting", async () => {
    const user = userEvent.setup();
    const client: ExportClient = {
      prepareExport: vi.fn().mockRejectedValue(new Error("session not found")),
      confirmExport: vi.fn(),
    };
    render(<ExportPanel sessionId="missing" client={client} />);

    await user.click(screen.getByTestId("prepare-export"));
    await waitFor(() => expect(screen.getByTestId("request-error")).toBeInTheDocument());
    expect(screen.getByTestId("request-error")).toHaveTextContent("session not found");
    expect(client.confirmExport).not.toHaveBeenCalled();
  });
});
