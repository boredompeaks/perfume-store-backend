import { describe, expect, it } from "vitest";
import { safeNext } from "./redirect";

describe("safeNext (open-redirect guard for ?next=)", () => {
  it("allows internal absolute paths", () => {
    expect(safeNext("/checkout")).toBe("/checkout");
    expect(safeNext("/products?ordering=-created_at")).toBe(
      "/products?ordering=-created_at",
    );
  });

  it("blocks absolute URLs", () => {
    expect(safeNext("https://evil.example")).toBeUndefined();
    expect(safeNext("http://evil.example/path")).toBeUndefined();
  });

  it("blocks protocol-relative URLs", () => {
    expect(safeNext("//evil.example")).toBeUndefined();
  });

  it("blocks backslash tricks that browsers normalize", () => {
    expect(safeNext("/\\evil.example")).toBeUndefined();
    expect(safeNext("\\evil.example")).toBeUndefined();
  });

  it("blocks empty / missing", () => {
    expect(safeNext("")).toBeUndefined();
    expect(safeNext(undefined)).toBeUndefined();
  });
});
