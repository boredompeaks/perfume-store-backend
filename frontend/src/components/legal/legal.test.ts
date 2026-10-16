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
 * A5.11 / A5.8 pins for the contact + legal surface (U-W8-T3). The pages are
 * static server components, so the guards read the source the way the build
 * ships it. Everything here is a rule the prose spec states in words and that
 * a refactor can otherwise break silently: the ToC's anchors, the phone-less
 * contact page staying form-less, and the §5.8 boundary token.
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
    // The nav renders `href={`#${section.id}`}` and the body renders
    // `id={section.id}` from the same prop — a hardcoded `href="#…"` or a
    // heading without its anchor would be a dead ToC link, which is the one
    // way this pattern actually fails.
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
    // §5.11: the ToC does not highlight the current section — no observer, no
    // scroll listener, no client island for a navigation aid.
    expect(proseSource).not.toMatch(
      /"use client"|IntersectionObserver|addEventListener|useState/,
    );
  });

  it("keeps the prose at the measure and body steps (§5.11, §2.2.4)", () => {
    expect(proseSource).toContain("max-w-measure");
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
    // The return window is the API's number or no number — never a typed one.
    expect(termsSource).toContain("{days ?");
    expect(termsSource).not.toMatch(/within \d+ days/);
  });

  it("introduces no new window, warranty or retention figure on either legal page", () => {
    for (const source of [termsSource, privacySource]) {
      // Any prose claim of the refs' class ("30 JOURS", "14 jours
      // calendaires") typed into the copy is the defect §0b exists to stop.
      expect(source).not.toMatch(/within \d+ (days|jours)/);
      expect(source).not.toMatch(/\b\d+[-–]\d+ (day|business day)/);
    }
  });
});

describe("contact (§5.8)", () => {
  it("stays channels-only: no form and no invented POST target", () => {
    // §5.8: there is no contact-submission endpoint; the real channels render
    // and that is the whole page. A form posting nowhere is the worst outcome.
    expect(contactSource).not.toMatch(/<form|onSubmit|\baction=/);
    expect(contactSource).toContain("<ContactLinks");
  });

  it("the contact button boundary is the control token, not the hairline", () => {
    // §5.8 CHANGE: `--border-field` instead of `--border-line`. Both classes
    // are real tokens, so this is a value pin, not a token-existence check.
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
    // These pages are 0% animated, not 2%: no scroll reveal, no entrance, no
    // route transition. `transition-colors`/`transition-all` on hover is
    // state feedback, not an accent moment, and is allowed.
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
