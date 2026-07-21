import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";

import { PrivateSourceNotice } from "./PrivateSourceNotice";

// The private-source usage-restriction notice (Req 25.6). It surfaces the
// backend-supplied notice when a draft is forked from the private production
// repository, and renders nothing for net-new / public-source sessions.
describe("PrivateSourceNotice (Req 25.6)", () => {
  it("renders the usage-restriction notice when one is supplied", () => {
    render(
      <PrivateSourceNotice notice="Internal use only; do not redistribute this fork." />,
    );
    const notice = screen.getByTestId("private-source-notice");
    expect(notice).toBeInTheDocument();
    expect(notice).toHaveAttribute("role", "note");
    expect(notice).toHaveTextContent(
      "Internal use only; do not redistribute this fork.",
    );
  });

  it("renders nothing when there is no notice", () => {
    const { container } = render(<PrivateSourceNotice notice={null} />);
    expect(container).toBeEmptyDOMElement();
    expect(screen.queryByTestId("private-source-notice")).not.toBeInTheDocument();
  });
});
