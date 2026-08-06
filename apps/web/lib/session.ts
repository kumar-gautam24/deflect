export const COOKIE_NAME = "deflect_session";

// The session token is a bearer credential: anything holding it is the user. httpOnly
// keeps it out of reach of scripts, so one XSS is a defaced page rather than a full
// account takeover. Lax rather than Strict because the login redirect is a top-level
// navigation, which Strict would block.
export function cookieOptions(maxAgeSeconds: number, env = process.env.NODE_ENV) {
  return {
    httpOnly: true,
    sameSite: "lax" as const,
    secure: env === "production",
    path: "/",
    maxAge: maxAgeSeconds,
  };
}
