import { TraceRow } from "@/components/trace-row";
import { type TraceSummary, getFromAnswer } from "@/lib/api";

export const dynamic = "force-dynamic";

export default async function TracesPage() {
  const traces = await getFromAnswer<TraceSummary[]>("/traces");

  return (
    <main className="mx-auto max-w-4xl space-y-6 p-8">
      <header className="space-y-1">
        <h1 className="text-2xl font-semibold">Traces</h1>
        <p className="text-muted-foreground text-sm">
          One row per request, with the chunks retrieved, their scores, and what the call cost.
        </p>
      </header>

      {traces.length === 0 ? (
        <p className="text-muted-foreground text-sm">No requests recorded yet.</p>
      ) : (
        <div>
          {traces.map((trace) => (
            <TraceRow key={trace.id} trace={trace} />
          ))}
        </div>
      )}
    </main>
  );
}
