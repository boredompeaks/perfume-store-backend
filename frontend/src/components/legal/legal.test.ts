import { readFileSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const srcDir = fileURLToPath(new URL("../../", import.meta.url));
const proseSource = readFileSync(
  join(srcDir, "components/legal/LegalProse.tsx"),
  "utf8",
);
const termsSource = readFileSync(join(srcDir, "app/terms/page.tsx"), "utf8");
const privacySource = readFileSync(
  join(srcDir, "app/privacy/page.tsx"),
  "utf8",
);
const contactSource = readFileSync(
  join(srcDir, "app/contact/page.tsx"),
  "utf8",
);
const contactLinksSource = readFileSync(
  join(srcDir, "components/ContactLinks.tsx"),
  "utf8",
);

/**
 * Source-level pins for the static legal/contact pages: ToC anchors, the
 * form-less contact page and the control-boundary token.
 */
function sectionIds(source: string): string[] {
  return [...source.matchAll(/^\s+id: "([a-z0-9-]+)",$/gm)].map((m) => m[1]);
}

describe("legal ToC (§5.11)", () => {
  it("terms and privacy each declare 5–7 uniquely anchored sections", () => {
    for (const [name, source] of [
      ["terms", termsSource],
      ["privacy", privacySource],
    ] as const) {
      const ids = sectionIds(source);
      expect(ids.length, name).toBeGreaterThanOrEqual(5);
      expect(ids.length, name).toBeLessThanOrEqual(7);
      expect(new Set(ids).size, name).toBe(ids.length);
    }
  });

  it("binds the ToC href and the section id to the same field, so they cannot drift", () => {
    // Href and id derive from one prop, so a hardcoded href or missing anchor
    // would be a dead ToC link.
    expect(proseSource).toContain("href={`#${section.id}`}");
    expect(proseSource).toContain("id={section.id}");
    expect(proseSource).toContain('aria-label="Table of contents"');
  });

  it("is a plain server-rendered nav — hidden on phones, sticky at lg, no scroll spy", () => {
    expect(proseSource).toContain("hidden");
    expect(proseSource).toContain("md:block");
    expect(proseSource).toContain("lg:sticky");
    expect(proseSource).toContain("lg:top-24");
    expect(proseSource).toContain("lg:grid-cols-[200px_1fr]");
    // No scroll spy: no observer, listener or client island for a nav aid.
    expect(proseSource).not.toMatch(
      /"use client"|IntersectionObserver|addEventListener|useState/,
    );
  });

  it("keeps the prose at the measure and body steps (§5.11, §2.2.4)", () => {
    expect(proseSource).toContain("max-w-(--measure)");
    expect(proseSource).toContain("text-body");
    expect(proseSource).toContain("text-h2");
  });

  it("keeps both pages inside LegalShell verbatim (§5.11)", () => {
    expect(termsSource).toContain("<LegalShell title=");
    expect(privacySource).toContain("<LegalShell title=");
    expect(termsSource).toContain("<LegalProse sections=");
    expect(privacySource).toContain("<LegalProse sections=");
  });
});

describe("legal copy discipline (P5, §0b.1)", () => {
  it("terms cross-links the shipped policy pages instead of restating their numbers", () => {
    expect(termsSource).toContain('href="/shipping"');
    expect(termsSource).toContain('href="/returns"');
    // The window is the API's number or no number, never a typed one.
    expect(termsSource).toContain("{days ?");
    expect(termsSource).not.toMatch(/within \d+ days/);
  });

  it("introduces no new window, warranty or retention figure on either legal page", () => {
    for (const source of [termsSource, privacySource]) {
      // A typed window in the copy is exactly the defect the rule stops.
      expect(source).not.toMatch(/within \d+ (days|jours)/);
      expect(source).not.toMatch(/\b\d+[-–]\d+ (day|business day)/);
    }
  });
});

describe("contact (§5.8)", () => {
  it("stays channels-only: no form and no invented POST target", () => {
    // No contact-submission endpoint exists, so the page is channels only.
    expect(contactSource).not.toMatch(/<form|onSubmit|\baction=/);
    expect(contactSource).toContain("<ContactLinks");
  });

  it("the contact button boundary is the control token, not the hairline", () => {
    // Both are real tokens; this pins the value, not token existence.
    expect(contactLinksSource).toContain("border-field-border");
    expect(contactLinksSource).not.toContain("border-hairline");
  });

  it("hides an unconfigured channel rather than rendering a dead link", () => {
    expect(contactLinksSource).toContain("if (settings.supportPhone)");
    expect(contactLinksSource).toContain("if (settings.whatsappNumber)");
    expect(contactLinksSource).toContain("if (settings.instagramUrl)");
  });
});

describe("motion (§5.12.4 rule 3, P2)", () => {
  it("declares no accent moment on any of the four surfaces", () => {
    // 0% animated: no reveal/entrance/route transition. Hover colour changes
    // are state feedback and allowed.
    for (const source of [
      proseSource,
      termsSource,
      privacySource,
      contactSource,
    ]) {
      expect(source).not.toMatch(
        /animate-|@keyframes|animation-timeline|settle|duration-/,
      );
    }
  });
});
