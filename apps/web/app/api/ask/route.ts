const ANSWER_URL = process.env.ANSWER_URL ?? "http://localhost:8002";

// The browser never holds a provider key. The stream is proxied so the model is
// only ever reachable from the FastAPI service.
export async function POST(request: Request) {
  const upstream = await fetch(`${ANSWER_URL}/ask`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: await request.text(),
  });

  return new Response(upstream.body, {
    status: upstream.status,
    headers: {
      "Content-Type": "text/event-stream",
      "Cache-Control": "no-cache",
    },
  });
}
