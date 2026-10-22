import { readFileSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const here = fileURLToPath(new URL(".", import.meta.url));
const emailOnlySource = readFileSync(join(here, "EmailOnlyForm.tsx"), "utf8");
const resetSource = readFileSync(join(here, "ResetPasswordClient.tsx"), "utf8");
const verifySource = readFileSync(join(here, "VerifyEmailClient.tsx"), "utf8");

const appDir = fileURLToPath(new URL("../../app", import.meta.url));
const pageSource = (route: string) =>
  readFileSync(join(appDir, route, "page.tsx"), "utf8");

/**
 * The uniform server answers (backend/accounts/views.py). The client must
 * never hardcode them — the message always arrives from the server and
 * renders verbatim — and must never grow copy that branches on whether an
 * account exists (uniform-response rule, conventions.md Security / §5.10).
 */
const UNIFORM_SERVER_COPIES = [
  "If an active account exists for this email",
  "If an account exists for this email",
  "If an unverified account exists",
] as const;

const CLIENT_SOURCES = {
  "EmailOnlyForm.tsx": emailOnlySource,
  "ResetPasswordClient.tsx": resetSource,
  "VerifyEmailClient.tsx": verifySource,
} as const;

describe("uniform-response discipline (§5.10)", () => {
  it("never hardcodes a uniform server message in the client", () => {
    for (const [file, source] of Object.entries(CLIENT_SOURCES)) {
      for (const copy of UNIFORM_SERVER_COPIES) {
        expect(source, `${file} must not contain: ${copy}`).not.toContain(copy);
      }
    }
  });

  it("EmailOnlyForm renders the server's message verbatim, never branched", () => {
    // The success panel renders the message state directly — no rewrite, no
    // substring, no conditional on its content.
    expect(emailOnlySource).toContain("{state.message}");
    expect(emailOnlySource).not.toMatch(/state\.message\s*[!=]==?/);
    expect(emailOnlySource).not.toMatch(/state\.message\.(includes|slice|replace)/);
  });

  it("EmailOnlyForm's success state is a field-border panel with a polite status", () => {
    // §5.10: the form→message swap is a visible container change.
    expect(emailOnlySource).toContain("border border-field-border p-8");
    expect(emailOnlySource).toContain('role="status" aria-live="polite"');
  });
});

describe("reset-password (§5.10)", () => {
  it("renders server failures and success verbatim", () => {
    // rejected: ApiError message straight through; done: server message.
    expect(resetSource).toContain("{state.message}");
    expect(resetSource).toContain("err.message");
    expect(resetSource).toContain("res.message");
  });

  it("renders validate_password errors per message, wired to the field", () => {
    // {password: [msgs]} → one <p> per message under the password input,
    // with the AddressForm aria wiring (the reference implementation).
    expect(resetSource).toContain("err.fieldErrors?.password");
    expect(resetSource).toContain("fieldErrors.map((msg) =>");
    expect(resetSource).toContain('htmlFor="reset-password"');
    expect(resetSource).toContain("aria-invalid={fieldErrors.length > 0");
    expect(resetSource).toContain(
      'aria-describedby={fieldErrors.length > 0 ? "reset-password-err"',
    );
  });

  it("keeps the missing-query state distinct from a server rejection", () => {
    expect(resetSource).toContain('kind: "incomplete"');
    expect(resetSource).toContain('kind: "rejected"');
    // uid && token gate — the form never posts without both.
    expect(resetSource).toContain("uid && token");
  });

  it("does not blame the link on a network failure", () => {
    // The non-ApiError path must not route into the rejected state.
    expect(resetSource).toContain("couldn't reach the server");
  });

  it("states errors in --color-danger, never the accent", () => {
    // CHANGE-2: bronze must never mean "this failed".
    expect(emailOnlySource).toContain('role="alert" className="text-sm text-danger"');
    expect(resetSource).not.toMatch(/role="alert"[^>]*text-bronze/);
    expect(resetSource).not.toMatch(/text-sm text-bronze/);
  });
});

describe("verify-email (§5.10)", () => {
  it("renders the server message verbatim on both outcomes", () => {
    expect(verifySource).toContain("{state.message}");
    expect(verifySource).toContain("res.message");
    expect(verifySource).toContain("err.message");
  });

  it("offers the resend link on the uniform 400", () => {
    expect(verifySource).toContain('href="/resend-verification"');
  });

  it("separates a network failure from a server rejection", () => {
    // A network failure must never claim the link is invalid.
    expect(verifySource).toContain('kind: "unreachable"');
    expect(verifySource).toContain("err instanceof ApiError");
    expect(verifySource).toContain("couldn&apos;t reach the server");
  });

  it("keeps the missing-query state distinct", () => {
    expect(verifySource).toContain('kind: "incomplete"');
    expect(verifySource).toContain("uid && token");
  });
});

describe("motion: none (§5.10)", () => {
  it("ships no raw millisecond durations and no animation utilities", () => {
    for (const [file, source] of Object.entries(CLIENT_SOURCES)) {
      expect(source, file).not.toMatch(/\b\d+ms\b/);
      expect(source, file).not.toContain("animate-");
      expect(source, file).not.toContain("@keyframes");
    }
  });
});

describe("recovery family parity (U-W13-T11 screens)", () => {
  it("closes every email-only column with the rule + link stack", () => {
    // Screen r_cup_ration_de_compte: hairline rule under the CTA, then the
    // underlined links. The stack renders under form AND sent panel.
    expect(emailOnlySource).toContain("border-t border-hairline");
    expect(emailOnlySource).toContain('href="/login"');
    expect(emailOnlySource).toContain('href="/contact"');
  });

  it("reset states keep the plate + full-width CTA + link stack", () => {
    // Screen nouveau_mot_de_passe: white plate on paper, full-width ink CTA
    // inside the panel, rule + links closing the column.
    expect(resetSource).toContain("border border-hairline bg-surface p-8");
    expect(resetSource).toContain("block w-full bg-ink");
    expect(resetSource).toContain("border-t border-hairline");
    expect(resetSource).toContain("Choose a new password");
  });

  it("verify states wear the register-panel status line (refined screen)", () => {
    // Refined v_rification_de_courriel: eyebrow status line over the inner
    // hairline; bronze only ever on the confirmed line, danger on the
    // refusal, never bronze on a failure (CHANGE-2).
    expect(verifySource).toContain("border border-hairline bg-surface p-8");
    expect(verifySource).toContain("text-eyebrow");
    expect(verifySource).toContain('statusLabel = "Status: confirmed"');
    expect(verifySource).toContain('statusLabel = "Status: unconfirmed"');
    expect(verifySource).toContain('statusTone = "text-bronze"');
    expect(verifySource).toContain('statusTone = "text-danger"');
    expect(verifySource).not.toMatch(/role="alert"[^>]*bronze/);
  });

  it("ships a full-width ink CTA in every client", () => {
    for (const [file, source] of Object.entries(CLIENT_SOURCES)) {
      expect(source, file).toContain("w-full bg-ink");
    }
  });
});

describe("recovery page wiring (§5.10, §8.1)", () => {
  it("posts the three email flows to the verified backend paths", () => {
    expect(pageSource("forgot-password")).toContain(
      'path="/api/accounts/password-reset/"',
    );
    expect(pageSource("forgot-username")).toContain(
      'path="/api/accounts/forgot-username/"',
    );
    expect(pageSource("resend-verification")).toContain(
      'path="/api/accounts/resend-verification/"',
    );
  });

  it("passes uid + token from the query to both token clients", () => {
    for (const route of ["reset-password", "verify-email"]) {
      const source = pageSource(route);
      expect(source, route).toContain("sp.uid");
      expect(source, route).toContain("sp.token");
    }
    expect(pageSource("reset-password")).toContain("ResetPasswordClient");
    expect(pageSource("verify-email")).toContain("VerifyEmailClient");
  });

  it("keeps all five routes noindex, nofollow (§8.1)", () => {
    for (const route of [
      "forgot-password",
      "forgot-username",
      "reset-password",
      "verify-email",
      "resend-verification",
    ]) {
      expect(pageSource(route), route).toContain("index: false, follow: false");
    }
  });
});
