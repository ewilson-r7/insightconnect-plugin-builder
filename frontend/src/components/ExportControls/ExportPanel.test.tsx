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
    // A `.plg` is a container image, so the fixture carries the identity a tenant reads.
    artifact: { image_tag: "rapid7_custom/acme:1.0.1", filename: "rapid7_custom_acme_1.0.1.plg" },
    diff: { added: ["plugin.spec.yaml", "help.md"], removed: [], modified: [], first_version: true },
    version_display: "1.0.0 -> 1.0.1",
    spec_errors: [],
    failed_stages: [],
    plugin_is_done: true,
    done_conditions: [],
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
        // Objects, as `_serialize_failed_stages` sends them. This case previously
        // passed strings, which is why the mismatch survived to task 14's browser
        // review: the fixture agreed with the type declaration and both disagreed
        // with the backend, so the whole panel unmounted on a real failed stage.
        failed_stages: [
          { name: "lint", status: "failed", returncode: 1, message: "2 findings" },
          { name: "test", status: "failed", returncode: 1, message: "1 test failed" },
        ],
      }),
    );
    render(<ExportPanel sessionId="s1" client={client} />);

    await user.click(screen.getByTestId("prepare-export"));
    await waitFor(() => expect(screen.getByTestId("export-blocked")).toBeInTheDocument());

    expect(screen.queryByTestId("confirm-controls")).not.toBeInTheDocument();
    expect(screen.getByTestId("blocked-spec-errors")).toHaveTextContent("connection.host");
    expect(screen.getByTestId("blocked-failed-stages")).toHaveTextContent("lint");
  });

  it("shows each failing stage's message and output, not just its name (clause 2.16)", async () => {
    const user = userEvent.setup();
    const client = makeClient(
      makePlan({
        permitted: false,
        summary: "Two stages failed.",
        failed_stages: [
          {
            name: "lint",
            status: "failed",
            returncode: 1,
            message: "2 lint finding(s) in hand-written code",
            displayed_output: "util/api.py:16: undefined-variable: Undefined variable 'requests'",
            full_output: "util/api.py:16: undefined-variable: Undefined variable 'requests'",
            truncated: false,
          },
        ],
      }),
    );
    render(<ExportPanel sessionId="s1" client={client} />);

    await user.click(screen.getByTestId("prepare-export"));
    await waitFor(() => expect(screen.getByTestId("export-blocked")).toBeInTheDocument());

    const stage = screen.getByTestId("failed-stage-lint");
    expect(stage).toHaveTextContent("2 lint finding(s) in hand-written code");
    expect(stage).toHaveTextContent("undefined-variable");
  });

  it("presents the blocked notice as a navigable region, not an assertive alert", async () => {
    const user = userEvent.setup();
    const client = makeClient(
      makePlan({
        permitted: false,
        summary: "Two stages failed.",
        failed_stages: [{ name: "lint", message: "2 findings" }],
      }),
    );
    render(<ExportPanel sessionId="s1" client={client} />);

    await user.click(screen.getByTestId("prepare-export"));
    await waitFor(() => expect(screen.getByTestId("export-blocked")).toBeInTheDocument());

    // An alert is assertive and atomic, so the stage output -- up to 10,000
    // characters -- would be read in full before the operator could act.
    const blocked = screen.getByTestId("export-blocked");
    expect(blocked).toHaveAttribute("role", "region");
    expect(blocked).toHaveAccessibleName("Export blocked");
  });

  it("names the outstanding conditions even when export is permitted (Req 27.2, 27.3)", async () => {
    // The dangerous case: the gate permits, so the operator sees the confirm
    // controls. Without this notice the preview would present an unfinished
    // plugin as ready to ship.
    const user = userEvent.setup();
    const client = makeClient(
      makePlan({
        permitted: true,
        plugin_is_done: false,
        done_conditions: [
          {
            name: "api_client",
            status: "unmet",
            description: "an API client centralizes requests",
            detail: "icon_acme/util/api.py does not exist",
          },
          {
            name: "lint_clean",
            status: "unverified",
            description: "the linter reports nothing",
            detail: "the check did not run: prospector (not available)",
          },
        ],
      }),
    );
    render(<ExportPanel sessionId="s1" client={client} />);

    await user.click(screen.getByTestId("prepare-export"));
    await waitFor(() => expect(screen.getByTestId("confirm-controls")).toBeInTheDocument());

    const notice = screen.getByTestId("done-outstanding");
    expect(notice).toHaveTextContent("not finished");
    // A checked-and-failed condition is listed apart from one nothing is known
    // about, so an absent linter does not read as a defect in the plugin.
    expect(screen.getByTestId("done-unmet")).toHaveTextContent("api_client");
    expect(screen.getByTestId("done-unmet")).toHaveTextContent("util/api.py does not exist");
    expect(screen.getByTestId("done-unverified")).toHaveTextContent("lint_clean");
    expect(screen.queryByTestId("done-met")).not.toBeInTheDocument();
  });

  it("confirms the definition of done is met when every condition holds", async () => {
    const user = userEvent.setup();
    const client = makeClient(makePlan({ plugin_is_done: true, done_conditions: [] }));
    render(<ExportPanel sessionId="s1" client={client} />);

    await user.click(screen.getByTestId("prepare-export"));
    await waitFor(() => expect(screen.getByTestId("confirm-controls")).toBeInTheDocument());

    expect(screen.getByTestId("done-met")).toBeInTheDocument();
    expect(screen.queryByTestId("done-outstanding")).not.toBeInTheDocument();
  });

  it("says nothing about the definition of done when it was not evaluated", async () => {
    // Not evaluated is not the same as not met; inventing a verdict here would be
    // the same defect as reporting a skipped check as a pass.
    const user = userEvent.setup();
    const client = makeClient(makePlan({ plugin_is_done: null, done_conditions: [] }));
    render(<ExportPanel sessionId="s1" client={client} />);

    await user.click(screen.getByTestId("prepare-export"));
    await waitFor(() => expect(screen.getByTestId("confirm-controls")).toBeInTheDocument());

    expect(screen.queryByTestId("done-met")).not.toBeInTheDocument();
    expect(screen.queryByTestId("done-outstanding")).not.toBeInTheDocument();
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

describe("the artifact the export would produce (Req 16.2)", () => {
  it("names the image tag, because that is what a tenant identifies the plugin by", async () => {
    const user = userEvent.setup();
    render(<ExportPanel sessionId="s1" client={makeClient(makePlan())} />);

    await user.click(screen.getByTestId("prepare-export"));
    await waitFor(() => expect(screen.getByTestId("artifact-summary")).toBeInTheDocument());

    expect(screen.getByTestId("artifact-image-tag")).toHaveTextContent("rapid7_custom/acme:1.0.1");
    expect(screen.getByTestId("artifact-filename")).toHaveTextContent("rapid7_custom_acme_1.0.1.plg");
  });

  it("stays silent when the spec cannot yet form an identity", async () => {
    // A missing name or version. The completeness findings beside the preview name the
    // missing field, so a half-formed tag here would be worse than nothing.
    const user = userEvent.setup();
    render(<ExportPanel sessionId="s1" client={makeClient(makePlan({ artifact: null }))} />);

    await user.click(screen.getByTestId("prepare-export"));
    await waitFor(() => expect(screen.getByTestId("export-preview")).toBeInTheDocument());

    expect(screen.queryByTestId("artifact-summary")).not.toBeInTheDocument();
  });

  it("describes the file list as the build context, not the archive's members", async () => {
    // The list used to be guaranteed equal to the .plg's members. It is now the build
    // context: the plugin's own .dockerignore may keep some of it out of the image.
    const user = userEvent.setup();
    render(<ExportPanel sessionId="s1" client={makeClient(makePlan())} />);

    await user.click(screen.getByTestId("prepare-export"));
    await waitFor(() => expect(screen.getByTestId("file-list")).toBeInTheDocument());

    expect(screen.getByTestId("file-list")).toHaveTextContent("Files the image is built from");
  });
});
