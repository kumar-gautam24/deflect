import { NextResponse } from "next/server";

import { COOKIE_NAME, cookieOptions } from "@/lib/session";

const AUTH_URL = process.env.AUTH_URL ?? "http://localhost:8004";

// The request body is forwarded untouched rather than parsed into {email, password} and
// re-serialised: the password never becomes a named value here, so there is nothing in
// this handler that could accidentally end up in a log line.
export async function POST(request: Request) {
  const upstream = await fetch(`${AUTH_URL}/auth/login`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: await request.text(),
  });

  // 401 and 423 are left as the auth service sent them, Retry-After included, so the
  // form can tell "wrong password" from "locked, try again in N minutes" instead of
  // collapsing both into one generic failure.
  if (!upstream.ok) {
    const retryAfter = upstream.headers.get("Retry-After");
    return NextResponse.json(await upstream.json(), {
      status: upstream.status,
      headers: retryAfter ? { "Retry-After": retryAfter } : {},
    });
  }

  const { token, expires_at } = await upstream.json();
  const maxAge = Math.max(0, Math.floor((Date.parse(expires_at) - Date.now()) / 1000));

  const response = NextResponse.json({ ok: true });
  response.cookies.set(COOKIE_NAME, token, cookieOptions(maxAge));
  return response;
}
