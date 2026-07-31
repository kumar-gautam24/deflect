const API_URL = process.env.API_URL ?? "http://localhost:8000";

// The browser never holds a provider key. The stream is proxied so the model is
// only ever reachable from the FastAPI service.
export async function POST(request: Request) {
  const upstream = await fetch(`${API_URL}/ask`, {
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
