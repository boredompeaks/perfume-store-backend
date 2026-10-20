import { describe, expect, it } from "vitest";
import { API_BASE, mediaUrl, resolveApiBase } from "./config";

describe("resolveApiBase", () => {
  it("derives the API origin from the page host, so the pair stays same-site", () => {
    expect(
      resolveApiBase(undefined, { protocol: "http:", hostname: "localhost" }),
    ).toBe("http://localhost:8000");
    expect(
      resolveApiBase(undefined, { protocol: "http:", hostname: "127.0.0.1" }),
    ).toBe("http://127.0.0.1:8000");
  });

  it("keeps the page scheme", () => {
    expect(
      resolveApiBase(undefined, { protocol: "https:", hostname: "shop.example" }),
    ).toBe("https://shop.example:8000");
  });

  it("falls back to loopback on the server, where there is no page", () => {
    expect(resolveApiBase(undefined, null)).toBe("http://localhost:8000");
  });

  it("lets an explicit override win, in the browser and on the server", () => {
    const override = "https://api.shop.example";
    expect(
      resolveApiBase(override, { protocol: "http:", hostname: "localhost" }),
    ).toBe(override);
    expect(resolveApiBase(override, null)).toBe(override);
  });
});

describe("API_BASE", () => {
  it("uses the server fallback when the module is imported without a window", () => {
    expect(API_BASE).toBe("http://localhost:8000");
  });
});

describe("mediaUrl", () => {
  it("prefixes relative media paths with the API origin", () => {
    expect(mediaUrl("/media/product.png")).toBe(
      "http://localhost:8000/media/product.png",
    );
  });

  it("leaves absolute URLs alone and maps empty values to null", () => {
    expect(mediaUrl("https://cdn.example/p.png")).toBe(
      "https://cdn.example/p.png",
    );
    expect(mediaUrl(null)).toBeNull();
    expect(mediaUrl(undefined)).toBeNull();
    expect(mediaUrl("")).toBeNull();
  });
});
