import { NextResponse, type NextRequest } from "next/server";
import { isAuthorized } from "@/lib/basic-auth";

// Basic auth rather than a sign-in page: the browser renders the credential prompt
// itself, so there is no session store and no cookie to get wrong. A login flow for a
// single operator is machinery maintained forever to avoid one browser prompt.
export function middleware(request: NextRequest) {
  if (isAuthorized(request.headers.get("authorization"), process.env.OPERATOR_TOKEN ?? "")) {
    return NextResponse.next();
  }

  return new NextResponse("authentication required", {
    status: 401,
    headers: { "WWW-Authenticate": 'Basic realm="deflect traces"' },
  });
}

// Both forms: the bare path is not covered by the :path* pattern.
export const config = { matcher: ["/traces", "/traces/:path*"] };
