// Extracted from the ask proxy so it can be unit-tested, and because getting this wrong
// silently disables rate limiting on the one endpoint that costs money.
//
// X-Forwarded-For arrives as a list and the visitor can set it. Depending on the
// platform their value is either overwritten or kept with the real address appended to
// its right, so the LEFTMOST entry is attacker-controlled either way and the rightmost
// is the one the nearest trusted proxy added. x-real-ip is preferred where the platform
// sets it, because it is a single value with no list to mis-parse.
export function clientAddress(headers: Headers): string | null {
  const realIp = headers.get("x-real-ip")?.trim();
  if (realIp) return realIp;

  const forwarded = headers.get("x-forwarded-for");
  if (!forwarded) return null;

  const hops = forwarded
    .split(",")
    .map((hop) => hop.trim())
    .filter(Boolean);

  return hops.length > 0 ? hops[hops.length - 1] : null;
}
