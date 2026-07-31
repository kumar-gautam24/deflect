import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { RunDiff } from "./run-diff";

const run = {
  id: 1,
  git_sha: "abc1234",
  prompt_version: "answer_v1",
  model: "gemini-2.0-flash",
  item_count: 80,
  metrics: { faithfulness: 0.9, hit_at_5: 0.8, deflection_rate: 0.7 },
  retrieval_config: {},
  created_at: "2026-07-31T00:00:00+00:00",
};

const worse = { ...run, id: 2, metrics: { ...run.metrics, faithfulness: 0.6 } };

describe("RunDiff", () => {
  it("lists regressed items with both scores", () => {
    render(
      <RunDiff
        diff={{
          base: run,
          head: worse,
          regressed: [
            {
              item_id: "q7",
              question: "How do background tasks work?",
              base_faithfulness: 1,
              head_faithfulness: 0.3,
            },
          ],
        }}
      />,
    );

    expect(screen.getByText("q7")).toBeDefined();
    expect(screen.getByText("1.00")).toBeDefined();
    expect(screen.getByText("0.30")).toBeDefined();
  });

  it("reports a clean diff when nothing regressed", () => {
    render(<RunDiff diff={{ base: run, head: { ...run, id: 2 }, regressed: [] }} />);

    expect(screen.getByText(/no regressions/i)).toBeDefined();
  });

  it("marks a metric that fell between the two runs", () => {
    render(<RunDiff diff={{ base: run, head: worse, regressed: [] }} />);

    expect(screen.getByTestId("metric-faithfulness").className).toContain("text-red");
  });

  it("marks a metric that held or improved", () => {
    render(<RunDiff diff={{ base: run, head: worse, regressed: [] }} />);

    expect(screen.getByTestId("metric-hit_at_5").className).toContain("text-emerald");
  });
});
