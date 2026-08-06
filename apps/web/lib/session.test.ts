import { describe, expect, it } from "vitest";
import { cookieOptions } from "./session";

describe("cookieOptions", () => {
  it("keeps the cookie away from scripts", () => {
    // The session token is a bearer credential. Readable by script, one XSS is a
    // full account takeover rather than a defaced page.
    expect(cookieOptions(3600).httpOnly).toBe(true);
  });

  it("does not send the cookie on cross-site requests", () => {
    expect(cookieOptions(3600).sameSite).toBe("lax");
  });

  it("expires with the session rather than outliving it", () => {
    expect(cookieOptions(3600).maxAge).toBe(3600);
  });

  it("is secure outside development", () => {
    expect(cookieOptions(3600, "production").secure).toBe(true);
  });

  it("is not secure in development, so http://localhost still works", () => {
    expect(cookieOptions(3600, "development").secure).toBe(false);
  });
});
