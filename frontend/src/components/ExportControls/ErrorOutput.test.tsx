import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { ErrorOutput } from "./ErrorOutput";

describe("ErrorOutput", () => {
  it("shows the bounded output and reveals full output on demand (Req 19.5)", async () => {
    const user = userEvent.setup();
    const full = "x".repeat(10_500);
    const displayed = full.slice(0, 10_000);
    render(
      <ErrorOutput
        failure={{ displayed_output: displayed, full_output: full, truncated: true }}
      />,
    );

    // Truncated view first: only the first 10,000 chars are shown.
    expect(screen.getByTestId("error-output-body").textContent).toHaveLength(10_000);
    expect(screen.getByTestId("error-output-truncated-note")).toHaveTextContent(
      "first 10000 of 10500",
    );

    // Full output remains accessible.
    await user.click(screen.getByTestId("error-output-show-full"));
    expect(screen.getByTestId("error-output-body").textContent).toHaveLength(10_500);

    await user.click(screen.getByTestId("error-output-collapse"));
    expect(screen.getByTestId("error-output-body").textContent).toHaveLength(10_000);
  });

  it("shows no truncation controls when the output is not truncated", () => {
    render(
      <ErrorOutput
        failure={{ displayed_output: "short error", full_output: "short error", truncated: false }}
      />,
    );

    expect(screen.getByTestId("error-output-body")).toHaveTextContent("short error");
    expect(screen.queryByTestId("error-output-truncation")).not.toBeInTheDocument();
  });
});
