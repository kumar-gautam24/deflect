// Extracted from the ask proxy so it can be unit-tested, and because getting this wrong
// silently disables rate limiting on the one endpoint that costs money.
//
// Only the RIGHTMOST hop of X-Forwarded-For is trusted. The header is a list the visitor
// can prepend to; the deployment platform either overwrites it or appends the real
// address to its right, so the rightmost entry is the one the nearest trusted proxy
// added and the leftmost is attacker-controlled either way.
//
// x-real-ip is deliberately NOT consulted. It is an nginx convention that neither Vercel
// nor Render documents setting or stripping, so trusting it would mean trusting a header
// nothing in this stack controls -- a visitor could simply send one.
//
// Behind no proxy at all (local development, or self-hosting without one) the only value
// present is the visitor's own, and it is spoofable. That is inherent to running without
// a trusted hop rather than something a header choice can fix; the daily cap is what
// bounds cost in that deployment.
export function clientAddress(headers: Headers): string | null {
  const forwarded = headers.get("x-forwarded-for");
  if (!forwarded) return null;

  const hops = forwarded
    .split(",")
    .map((hop) => hop.trim())
    .filter(Boolean);

  return hops.length > 0 ? hops[hops.length - 1] : null;
}
