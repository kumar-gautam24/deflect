import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { AnswerPanel } from "./answer-panel";

const done = {
  type: "done" as const,
  trace_id: 1,
  escalated: false,
  reason: null,
  citations: [{ source_path: "deps.md", heading_path: "Dependencies", chunk_id: 7 }],
  latency_ms: 120,
};

describe("AnswerPanel", () => {
  it("renders the answer and its citations", () => {
    render(<AnswerPanel answer="Use Depends." done={done} />);

    expect(screen.getByText("Use Depends.")).toBeDefined();
    expect(screen.getByText("Dependencies")).toBeDefined();
  });

  it("shows an escalation notice with its reason instead of citations", () => {
    render(
      <AnswerPanel
        answer="Not covered."
        done={{
          ...done,
          escalated: true,
          reason: "low_retrieval_score",
          citations: [],
        }}
      />,
    );

    expect(screen.getByText(/escalated to a human/i)).toBeDefined();
    expect(screen.getByText(/low_retrieval_score/)).toBeDefined();
  });

  it("hides citations when the answer was escalated even if some were returned", () => {
    render(
      <AnswerPanel
        answer="Not covered."
        done={{ ...done, escalated: true, reason: "ungrounded_answer" }}
      />,
    );

    expect(screen.queryByText("Dependencies")).toBeNull();
  });

  it("renders only the streaming answer before the done event arrives", () => {
    render(<AnswerPanel answer="Use " done={null} />);

    expect(screen.getByText("Use")).toBeDefined();
    expect(screen.queryByText(/escalated/i)).toBeNull();
    expect(screen.queryByText(/trace/)).toBeNull();
  });
});
