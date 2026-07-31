import { Badge } from "@/components/ui/badge";
import type { TraceSummary } from "@/lib/api";

export function TraceRow({ trace }: { trace: TraceSummary }) {
  return (
    <details className="border-t py-3">
      <summary className="flex cursor-pointer items-center justify-between gap-4 text-sm">
        <span className="truncate">{trace.question}</span>
        <span className="text-muted-foreground flex shrink-0 items-center gap-3">
          {trace.escalated && <Badge variant="outline">{trace.reason}</Badge>}
          <span>{trace.latency_ms} ms</span>
          <span>${trace.cost_usd.toFixed(5)}</span>
        </span>
      </summary>

      <dl className="text-muted-foreground mt-3 grid grid-cols-2 gap-2 text-xs sm:grid-cols-4">
        <div>
          <dt>Top score</dt>
          <dd>{trace.top_score.toFixed(3)}</dd>
        </div>
        <div>
          <dt>Margin</dt>
          <dd>{trace.margin.toFixed(3)}</dd>
        </div>
        <div>
          <dt>Tokens in</dt>
          <dd>{trace.input_tokens}</dd>
        </div>
        <div>
          <dt>Tokens out</dt>
          <dd>{trace.output_tokens}</dd>
        </div>
      </dl>

      <ol className="mt-3 space-y-1 text-xs">
        {trace.retrieved.map((chunk) => (
          <li key={chunk.chunk_id} className="flex justify-between gap-4">
            <span className="font-mono">{chunk.source_path}</span>
            <span className="text-muted-foreground">{chunk.score.toFixed(3)}</span>
          </li>
        ))}
      </ol>
    </details>
  );
}
