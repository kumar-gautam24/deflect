import { Badge } from "@/components/ui/badge";
import { Card } from "@/components/ui/card";
import type { AskDone } from "@/lib/api";

export function AnswerPanel({ answer, done }: { answer: string; done: AskDone | null }) {
  return (
    <Card className="space-y-4 p-6">
      <p className="whitespace-pre-wrap leading-relaxed">{answer}</p>

      {done?.escalated && (
        <div className="rounded-md border border-amber-500/40 bg-amber-500/5 p-3 text-sm">
          <p className="font-medium">Escalated to a human</p>
          <p className="text-muted-foreground">Reason: {done.reason}</p>
        </div>
      )}

      {done && !done.escalated && done.citations.length > 0 && (
        <div className="space-y-2">
          <p className="text-muted-foreground text-xs tracking-wide uppercase">Sources</p>
          <div className="flex flex-wrap gap-2">
            {done.citations.map((citation) => (
              <Badge key={citation.chunk_id} variant="secondary" title={citation.source_path}>
                {citation.heading_path}
              </Badge>
            ))}
          </div>
        </div>
      )}

      {done && (
        <p className="text-muted-foreground text-xs">
          {done.latency_ms} ms &middot; trace {done.trace_id}
        </p>
      )}
    </Card>
  );
}
