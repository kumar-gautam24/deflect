import type { EvalRunSummary } from "@/lib/api";

export type DiffResponse = {
  base: EvalRunSummary;
  head: EvalRunSummary;
  regressed: {
    item_id: string;
    question: string;
    base_faithfulness: number;
    head_faithfulness: number;
  }[];
};

export function RunDiff({ diff }: { diff: DiffResponse }) {
  const names = Object.keys(diff.base.metrics);

  return (
    <section className="space-y-6">
      <div className="grid grid-cols-2 gap-4 sm:grid-cols-3">
        {names.map((name) => {
          const base = diff.base.metrics[name];
          const head = diff.head.metrics[name];
          return (
            <div key={name} className="rounded-md border p-3">
              <p className="text-muted-foreground text-xs uppercase">{name}</p>
              <p
                data-testid={`metric-${name}`}
                className={head < base ? "text-red-500" : "text-emerald-500"}
              >
                {head.toFixed(2)}
                <span className="text-muted-foreground ml-2 text-xs">from {base.toFixed(2)}</span>
              </p>
            </div>
          );
        })}
      </div>

      {diff.regressed.length === 0 ? (
        <p className="text-muted-foreground text-sm">No regressions.</p>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="text-muted-foreground text-left">
                <th className="py-2">Item</th>
                <th>Question</th>
                <th>Base</th>
                <th>Head</th>
              </tr>
            </thead>
            <tbody>
              {diff.regressed.map((item) => (
                <tr key={item.item_id} className="border-t">
                  <td className="py-2 font-mono text-xs">{item.item_id}</td>
                  <td>{item.question}</td>
                  <td>{item.base_faithfulness.toFixed(2)}</td>
                  <td className="text-red-500">{item.head_faithfulness.toFixed(2)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
