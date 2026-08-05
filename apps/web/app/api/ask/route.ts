import { clientAddress } from "@/lib/client-ip";

const ANSWER_URL = process.env.ANSWER_URL ?? "http://localhost:8002";
const SERVICE_TOKEN = process.env.SERVICE_TOKEN ?? "";

// The browser never holds a provider key. The stream is proxied so the model is
// only ever reachable from the FastAPI service.
export async function POST(request: Request) {
  // The service token is not what authorises the question -- /ask is open. It is what
  // makes the forwarded address believable: without it the answer service would fall
  // back to this proxy's own address and rate limit every visitor as one caller.
  //
  // The address is computed and overwritten here, never relayed. X-Forwarded-For is a
  // header the visitor can set; passing their value through with this service's token
  // attached would let them mint a fresh rate-limit key per request, which is the whole
  // thing the limiter exists to prevent.
  const address = clientAddress(request.headers);

  // An unset token is not a broken question -- it is a deployment that will silently
  // rate limit every visitor as one caller, because the answer service will not trust
  // the address this proxy forwards. Failing here makes that a visible 500 in the
  // deploy's first minute rather than a throttle nobody diagnoses.
  if (!SERVICE_TOKEN) {
    return new Response("ask proxy is not configured: SERVICE_TOKEN is unset", {
      status: 500,
    });
  }

  const upstream = await fetch(`${ANSWER_URL}/ask`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${SERVICE_TOKEN}`,
      ...(address ? { "X-Forwarded-For": address } : {}),
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
