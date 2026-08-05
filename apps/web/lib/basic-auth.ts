// Extracted from proxy.ts so it can be unit-tested outside the edge runtime.
// The traces surface records every visitor's question and what it cost, so it is the
// one page that must not be public on an open demo.

function constantTimeEquals(a: string, b: string): boolean {
  // Length is compared first and leaks, which is acceptable: the length of an
  // operator token is not the secret. The loop keeps the content comparison
  // independent of how many leading characters matched.
  if (a.length !== b.length) return false;

  let difference = 0;
  for (let i = 0; i < a.length; i++) {
    difference |= a.charCodeAt(i) ^ b.charCodeAt(i);
  }
  return difference === 0;
}

export function isAuthorized(header: string | null, expected: string): boolean {
  // An unset OPERATOR_TOKEN must never authorise anyone. Without this an empty
  // environment variable would make every empty password correct.
  if (!expected) return false;
  if (!header) return false;

  const [scheme, encoded] = header.split(" ");
  if (scheme !== "Basic" || !encoded) return false;

  let decoded: string;
  try {
    decoded = atob(encoded);
  } catch {
    return false;
  }

  // Only the first colon separates user from password; the rest belong to the password.
  const separator = decoded.indexOf(":");
  if (separator === -1) return false;

  return constantTimeEquals(decoded.slice(separator + 1), expected);
}
