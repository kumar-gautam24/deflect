import { describe, expect, it } from "vitest";
import { isAuthorized } from "./basic-auth";

const encode = (user: string, password: string) =>
  `Basic ${Buffer.from(`${user}:${password}`).toString("base64")}`;

describe("isAuthorized", () => {
  it("accepts the expected password regardless of username", () => {
    expect(isAuthorized(encode("operator", "s3cret"), "s3cret")).toBe(true);
    expect(isAuthorized(encode("", "s3cret"), "s3cret")).toBe(true);
  });

  it("rejects a wrong password", () => {
    expect(isAuthorized(encode("operator", "wrong"), "s3cret")).toBe(false);
  });

  it("rejects a missing header", () => {
    expect(isAuthorized(null, "s3cret")).toBe(false);
  });

  it("rejects a non-Basic scheme", () => {
    expect(isAuthorized("Bearer s3cret", "s3cret")).toBe(false);
  });

  it("rejects malformed base64 without throwing", () => {
    expect(isAuthorized("Basic !!!not-base64!!!", "s3cret")).toBe(false);
  });

  it("rejects a password containing the expected one as a prefix", () => {
    expect(isAuthorized(encode("operator", "s3cretplus"), "s3cret")).toBe(false);
  });

  it("keeps a colon in the password intact", () => {
    expect(isAuthorized(encode("operator", "a:b"), "a:b")).toBe(true);
  });

  it("never authorises against an empty expected password", () => {
    expect(isAuthorized(encode("operator", ""), "")).toBe(false);
  });
});
