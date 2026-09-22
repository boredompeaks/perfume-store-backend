import { describe, expect, it } from "vitest";
import { toJsonLdScriptContent } from "./json-ld";

const parse = (json: string): unknown => JSON.parse(json);

describe("toJsonLdScriptContent (script-context escaping for JSON-LD)", () => {
  it("does not emit a literal closing-script sequence, even for a product name containing one", () => {
    const payload = {
      "@context": "https://schema.org",
      name: `Evil</script><script>alert("xss")</script>`,
    };
    const serialized = toJsonLdScriptContent(payload);
    expect(serialized.toLowerCase()).not.toContain("</script>");
    expect(serialized).not.toContain("<script");
    // JSON semantics are unchanged: the escape decodes back to the source.
    expect(parse(serialized)).toEqual(payload);
  });

  it("escapes '<' and '>' so no HTML tag can be opened inside the script element", () => {
    const serialized = toJsonLdScriptContent({ name: "a<b>c&d" });
    expect(serialized).toBe('{"name":"a\\u003cb\\u003ec\\u0026d"}');
    expect(parse(serialized)).toEqual({ name: "a<b>c&d" });
  });

  it("escapes the U+2028/U+2029 line separators that break JS parsing", () => {
    const serialized = toJsonLdScriptContent({ name: "line\u2028sep\u2029end" });
    expect(serialized).toBe(
      '{"name":"line\\u2028sep\\u2029end"}',
    );
    expect(parse(serialized)).toEqual({ name: "line\u2028sep\u2029end" });
  });

  it("escapes '<' reached through JSON-stringified nested objects", () => {
    const serialized = toJsonLdScriptContent({
      brand: { name: "</script>" },
      tags: ["<", ">"],
    });
    expect(serialized).toBe(
      '{"brand":{"name":"\\u003c/script\\u003e"},"tags":["\\u003c","\\u003e"]}',
    );
  });

  it("pins the homepage Organization payload: hostile site strings emit no script-terminating markup", () => {
    // Mirrors src/app/page.tsx's Organization JSON-LD shape (name/url/
    // description) — the homepage is the second ld+json sink and must get
    // the same escaping guarantee as the product page.
    const organizationJsonLd = {
      "@context": "https://schema.org",
      "@type": "Organization",
      name: `Evil</script><script>alert("xss")</script>`,
      url: "https://example.test/",
      description: "a<b>c&d\u2028line\u2029sep",
    };
    const serialized = toJsonLdScriptContent(organizationJsonLd);
    expect(serialized.toLowerCase()).not.toContain("</script>");
    expect(serialized).not.toContain("<script");
    // JSON semantics are unchanged: the escape decodes back to the source.
    expect(parse(serialized)).toEqual(organizationJsonLd);
  });

  it("leaves innocent payloads untouched", () => {
    const payload = {
      "@context": "https://schema.org",
      "@type": "Product",
      name: "Oud Royale",
      price: 4999,
    };
    expect(toJsonLdScriptContent(payload)).toBe(JSON.stringify(payload));
  });
});
