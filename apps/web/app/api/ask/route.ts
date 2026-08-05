const ANSWER_URL = process.env.ANSWER_URL ?? "http://localhost:8002";
const SERVICE_TOKEN = process.env.SERVICE_TOKEN ?? "";

// The browser never holds a provider key. The stream is proxied so the model is
// only ever reachable from the FastAPI service.
export async function POST(request: Request) {
  // The service token is not what authorises the question -- /ask is open. It is what
  // makes the forwarded address believable: without it the answer service would fall
  // back to this proxy's own address and rate limit every visitor as one caller.
  const forwardedFor = request.headers.get("x-forwarded-for");

  const upstream = await fetch(`${ANSWER_URL}/ask`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${SERVICE_TOKEN}`,
      ...(forwardedFor ? { "X-Forwarded-For": forwardedFor } : {}),
    },
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
