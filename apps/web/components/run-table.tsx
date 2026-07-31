import type { EvalRunSummary } from "@/lib/api";

function score(value: number | undefined): string {
  return value === undefined ? "-" : value.toFixed(2);
}

export function RunTable({ runs }: { runs: EvalRunSummary[] }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead>
          <tr className="text-muted-foreground text-left">
            <th className="py-2">Run</th>
            <th>Commit</th>
            <th>Prompt</th>
            <th>Items</th>
            <th>Faithfulness</th>
            <th>Hit@5</th>
            <th>Answered</th>
          </tr>
        </thead>
        <tbody>
          {runs.map((run) => (
            <tr key={run.id} className="border-t">
              <td className="py-2">{run.id}</td>
              <td className="font-mono text-xs">{run.git_sha.slice(0, 7)}</td>
              <td>{run.prompt_version}</td>
              <td>{run.item_count}</td>
              <td>{score(run.metrics.faithfulness)}</td>
              <td>{score(run.metrics.hit_at_5)}</td>
              <td>{score(run.metrics.answered_rate)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
