import { COOKIE_NAME } from "@/lib/session";

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
  // An error response still has a body, just no SSE frames in it. Without this check
  // the loop below would yield nothing and the caller would render a silent blank.
  if (!response.ok) throw new Error(`the API returned ${response.status}`);
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

// Server components go through the gateway now, the same as the client-rendered ask
// page: one base URL rather than one per upstream, and the credential each function
// forwards is what the gateway's route table actually checks for that path.
const GATEWAY_URL = process.env.GATEWAY_URL ?? "http://localhost:8000";

async function getJSONFrom<T>(base: string, path: string, token?: string): Promise<T> {
  const response = await fetch(`${base}${path}`, {
    cache: "no-store",
    headers: token ? { Authorization: `Bearer ${token}` } : {},
  });
  if (!response.ok) throw new Error(`${path} returned ${response.status}`);
  return response.json();
}

// The traces surface shows data because the person asking is allowed to see it, not
// because the server holds a master key. Forwarding the caller's own credential is what
// makes the audit trail meaningful.
//
// next/headers is imported dynamically, not at module scope: this file also exports
// askStream, which the client-rendered ask page imports, and a static import of
// next/headers pulls it into that browser bundle too, which Turbopack refuses to build.
// A dynamic import keeps it out of any bundle that never calls this function.
export async function getFromAnswer<T>(path: string): Promise<T> {
  const { cookies } = await import("next/headers");
  const token = (await cookies()).get(COOKIE_NAME)?.value;
  return getJSONFrom<T>(GATEWAY_URL, path, token);
}

// The eval dashboard is public and needs no credential.
export function getFromEvals<T>(path: string): Promise<T> {
  return getJSONFrom<T>(GATEWAY_URL, path);
}
