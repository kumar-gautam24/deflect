import { cookies } from "next/headers";
import { NextResponse } from "next/server";

import { COOKIE_NAME, cookieOptions } from "@/lib/session";

const GATEWAY_URL = process.env.GATEWAY_URL ?? "http://localhost:8000";

// A user who clicks log out must end up logged out of this browser even if the auth
// service is unreachable, so the upstream revoke is attempted but never allowed to
// gate the local cookie clear below.
export async function POST() {
  const token = (await cookies()).get(COOKIE_NAME)?.value;

  if (token) {
    try {
      await fetch(`${GATEWAY_URL}/auth/logout`, {
        method: "POST",
        headers: { Authorization: `Bearer ${token}` },
      });
    } catch {
      // Auth service unreachable -- the cookie is still cleared below.
    }
  }

  const response = NextResponse.json({ ok: true });
  response.cookies.set(COOKIE_NAME, "", cookieOptions(0));
  return response;
}
