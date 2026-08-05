import { describe, expect, it } from "vitest";
import { clientAddress } from "./client-ip";

const headers = (init: Record<string, string>) => new Headers(init);

describe("clientAddress", () => {
  it("ignores x-real-ip, which nothing in this stack is known to set", () => {
    const h = headers({ "x-real-ip": "9.9.9.9", "x-forwarded-for": "203.0.113.7" });
    expect(clientAddress(h)).toBe("203.0.113.7");
  });

  it("returns null when only x-real-ip is present, so no key is derived from it", () => {
    expect(clientAddress(headers({ "x-real-ip": "9.9.9.9" }))).toBeNull();
  });

  it("takes the rightmost hop, which the nearest trusted proxy added", () => {
    const h = headers({ "x-forwarded-for": "1.2.3.4, 203.0.113.7" });
    expect(clientAddress(h)).toBe("203.0.113.7");
  });

  it("ignores an address the visitor prefixed to evade the limiter", () => {
    const h = headers({ "x-forwarded-for": "9.9.9.9, 8.8.8.8, 203.0.113.7" });
    expect(clientAddress(h)).toBe("203.0.113.7");
  });

  it("handles a single forwarded value", () => {
    expect(clientAddress(headers({ "x-forwarded-for": "203.0.113.7" }))).toBe("203.0.113.7");
  });

  it("strips whitespace", () => {
    expect(clientAddress(headers({ "x-forwarded-for": "1.2.3.4 ,  203.0.113.7  " }))).toBe(
      "203.0.113.7",
    );
  });

  it("returns null when no address header is present", () => {
    expect(clientAddress(headers({}))).toBeNull();
  });

  it("returns null for a header that is only separators", () => {
    expect(clientAddress(headers({ "x-forwarded-for": " , , " }))).toBeNull();
  });
});
