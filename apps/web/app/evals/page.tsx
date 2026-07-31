import { RunDiff, type DiffResponse } from "@/components/run-diff";
import { RunTable } from "@/components/run-table";
import { type EvalRunSummary, getJSON } from "@/lib/api";

export const dynamic = "force-dynamic";

export default async function EvalsPage() {
  const runs = await getJSON<EvalRunSummary[]>("/eval-runs");
  const diff =
    runs.length >= 2
      ? await getJSON<DiffResponse>(`/eval-runs/diff?base=${runs[1].id}&head=${runs[0].id}`)
      : null;

  return (
    <main className="mx-auto max-w-5xl space-y-10 p-8">
      <header className="space-y-1">
        <h1 className="text-2xl font-semibold">Eval runs</h1>
        <p className="text-muted-foreground text-sm">
          Every run records its commit, prompt version and retrieval config, so a regression can be
          traced to the change that caused it.
        </p>
      </header>

      {runs.length === 0 ? (
        <p className="text-muted-foreground text-sm">
          No runs yet. Run <code>scripts/run_evals.py</code> to record one.
        </p>
      ) : (
        <RunTable runs={runs} />
      )}

      {diff && (
        <section className="space-y-4">
          <h2 className="text-lg font-medium">Latest run against the previous one</h2>
          <RunDiff diff={diff} />
        </section>
      )}
    </main>
  );
}
