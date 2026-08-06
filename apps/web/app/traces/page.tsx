import { redirect } from "next/navigation";

import { TraceRow } from "@/components/trace-row";
import { type TraceSummary, getFromAnswer } from "@/lib/api";

export const dynamic = "force-dynamic";

export default async function TracesPage() {
  let traces: TraceSummary[];
  try {
    traces = await getFromAnswer<TraceSummary[]>("/traces");
  } catch (cause) {
    // With proxy.ts gone, this is what stops an anonymous visitor from seeing a stack
    // trace instead of a login form: no session cookie means the answer service sends
    // 401, and that is the one failure this page handles rather than lets crash.
    if (cause instanceof Error && cause.message.includes("returned 401")) redirect("/login");
    throw cause;
  }

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
