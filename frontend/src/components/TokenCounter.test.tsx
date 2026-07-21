import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";

import { TokenCounter } from "./TokenCounter";

// The live cumulative session token counter (Req 3.6). It must always present a
// non-negative integer, grouped for readability, and never leak a transient
// negative/NaN value into the display.
describe("TokenCounter (Req 3.6)", () => {
  it("renders the cumulative total with an accessible label", () => {
    render(<TokenCounter total={0} />);
    const region = screen.getByLabelText("Cumulative session token usage");
    expect(region).toBeInTheDocument();
    expect(screen.getByTestId("token-total")).toHaveTextContent("0");
  });

  it("formats large totals with locale grouping", () => {
    render(<TokenCounter total={1234567} />);
    expect(screen.getByTestId("token-total")).toHaveTextContent(
      (1234567).toLocaleString(),
    );
  });

  it("floors fractional totals to a whole number of tokens", () => {
    render(<TokenCounter total={42.9} />);
    expect(screen.getByTestId("token-total")).toHaveTextContent("42");
  });

  it("clamps a transient negative value to zero", () => {
    render(<TokenCounter total={-5} />);
    expect(screen.getByTestId("token-total")).toHaveTextContent("0");
  });

  it("clamps a non-finite value to zero", () => {
    render(<TokenCounter total={Number.NaN} />);
    expect(screen.getByTestId("token-total")).toHaveTextContent("0");
  });
});
