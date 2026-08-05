import { describe, expect, it } from "vitest";
import { clientAddress } from "./client-ip";

const headers = (init: Record<string, string>) => new Headers(init);

describe("clientAddress", () => {
  it("prefers x-real-ip, which the platform sets as a single value", () => {
    expect(clientAddress(headers({ "x-real-ip": "203.0.113.7" }))).toBe("203.0.113.7");
  });

  it("ignores a visitor-supplied forwarded list when x-real-ip is present", () => {
    const h = headers({ "x-real-ip": "203.0.113.7", "x-forwarded-for": "1.2.3.4" });
    expect(clientAddress(h)).toBe("203.0.113.7");
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
