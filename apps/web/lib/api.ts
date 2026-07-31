export type Citation = {
  source_path: string;
  heading_path: string;
  chunk_id: number;
};

export type AskDone = {
  type: "done";
  trace_id: number;
  escalated: boolean;
  reason: string | null;
  citations: Citation[];
  latency_ms: number;
};

export type AskEvent = { type: "token"; text: string } | AskDone;

export type EvalRunSummary = {
  id: number;
  git_sha: string;
  prompt_version: string;
  model: string;
  item_count: number;
  metrics: Record<string, number>;
  retrieval_config: Record<string, unknown>;
  created_at: string;
};

export type TraceSummary = {
  id: number;
  question: string;
  answer: string;
  escalated: boolean;
  reason: string | null;
  top_score: number;
  margin: number;
  retrieved: { chunk_id: number; source_path: string; score: number }[];
  input_tokens: number;
  output_tokens: number;
  cost_usd: number;
  model: string;
  latency_ms: number;
  created_at: string;
};

export async function* askStream(question: string): AsyncGenerator<AskEvent> {
  const response = await fetch("/api/ask", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ question }),
  });
  if (!response.body) throw new Error("ask endpoint returned no body");

  const reader = response.body.pipeThrough(new TextDecoderStream()).getReader();
  let buffer = "";

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += value;

    // A frame can arrive split across reads, so the trailing partial is kept back
    // for the next chunk rather than parsed.
    const frames = buffer.split("\n\n");
    buffer = frames.pop() ?? "";
    for (const frame of frames) {
      if (frame.startsWith("data:")) yield JSON.parse(frame.slice(5));
    }
  }
}

const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

export async function getJSON<T>(path: string): Promise<T> {
  const response = await fetch(`${API_URL}${path}`, { cache: "no-store" });
  if (!response.ok) throw new Error(`${path} returned ${response.status}`);
  return response.json();
}
