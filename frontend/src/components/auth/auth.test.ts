import { readFileSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

/**
 * §5.9 compliance for the two storefront auth doors (/login, /register).
 * Source-level on purpose: vitest runs in the node environment (no DOM), so
 * these pin the load-bearing JSX facts a redesign silently breaks — the
 * wordmark ADD, the CHANGE-2 error colour, aria-invalid/aria-describedby
 * field mapping, verbatim server copy on the account-created states, the
 * unconditional recovery links, and "motion: none on any auth page".
 */

const authDir = fileURLToPath(new URL(".", import.meta.url));
const appDir = join(authDir, "../../app");

const read = (path: string) => readFileSync(path, "utf8");

const authShell = read(join(authDir, "AuthShell.tsx"));
const loginForm = read(join(authDir, "LoginForm.tsx"));
const registerForm = read(join(authDir, "RegisterForm.tsx"));
const loginPage = read(join(appDir, "login/page.tsx"));
const registerPage = read(join(appDir, "register/page.tsx"));

/** Every auth file this task owns — the no-motion and no-hex checks sweep them. */
const ownedSources = [authShell, loginForm, registerForm, loginPage, registerPage];

describe("AuthShell (§5.9 shared shell)", () => {
  it("renders the wordmark from the white-label manifest, never a literal", () => {
    expect(authShell).toContain("{site.name}");
    expect(authShell).not.toContain("Maison Aurel");
  });

  it("keeps the h1 on the --text-h1 step and the wordmark on --text-h3, centred", () => {
    expect(authShell).toMatch(/<h1[^>]*font-display text-h1/);
    expect(authShell).toMatch(/text-center font-display text-h3/);
  });

  it("stays a max-w-md centred column", () => {
    expect(authShell).toContain("mx-auto max-w-md");
  });
});

describe("LoginForm (§5.9 customer door)", () => {
  it("shows failure copy in --color-danger, never the bronze accent (CHANGE-2)", () => {
    expect(loginForm).toMatch(/role="alert"[^>]*text-danger/);
    expect(loginForm).not.toMatch(/role="alert"[^>]*text-bronze/);
  });

  it("renders every recovery link unconditionally (never branched on the error)", () => {
    for (const href of [
      "/staff/login",
      "/register",
      "/resend-verification",
      "/forgot-username",
      "/forgot-password",
    ]) {
      expect(loginForm).toContain(`href="${href}"`);
    }
    // The links must not live under an error-conditional block: extract the
    // first `error && ( … )}` JSX block and assert it carries no href.
    const errorIdx = loginForm.indexOf("error && (");
    expect(errorIdx).toBeGreaterThan(-1);
    const blockEnd = loginForm.indexOf(")}", errorIdx);
    expect(blockEnd).toBeGreaterThan(errorIdx);
    expect(loginForm.slice(errorIdx, blockEnd)).not.toMatch(/href=/);
  });

  it("keeps the TOTP input behind the staff variant only (SPEC-20-10)", () => {
    const totpIdx = loginForm.indexOf('id="login-totp"');
    expect(totpIdx).toBeGreaterThan(-1);
    const guard = loginForm.lastIndexOf("variant === ", totpIdx);
    expect(guard).toBeGreaterThan(-1);
    expect(loginForm.slice(guard, guard + 19)).toBe('variant === "staff"');
    expect(loginPage).toContain('variant="customer"');
    expect(loginPage).not.toContain('variant="staff"');
  });

  it("reports the server's message verbatim on the customer door", () => {
    // ApiError.message carries the envelope's `error` string (401 ambiguity,
    // 429 throttle wording). staff → fieldErrors.totp[0] ?? err.message;
    // customer → err.message, unwrapped; non-ApiError → network fallback.
    expect(loginForm).toContain('variant === "staff"');
    expect(loginForm).toContain("err.fieldErrors?.totp?.[0] ?? err.message");
    expect(loginForm).toMatch(/err\.message\)\s*:\s*err\.message/);
    expect(loginForm).toContain("Sign-in failed. Check your connection and try again.");
  });

  it("preserves the guest-wall next handoff (login → next ?? home)", () => {
    expect(loginPage).toContain("safeNext(");
    expect(loginPage).toContain("next={next}");
    expect(loginForm).toContain("router.push(next ?? \"/\")");
  });
});

describe("RegisterForm (§5.9)", () => {
  it("maps server field errors onto fields via aria-invalid + aria-describedby", () => {
    expect(registerForm.match(/aria-invalid=/g)?.length).toBe(3);
    for (const id of ["reg-username-err", "reg-email-err", "reg-password-err"]) {
      expect(registerForm).toContain(`"${id}"`);
    }
    // The hint stays wired alongside the username error description.
    expect(registerForm).toContain("reg-username-hint reg-username-err");
  });

  it("renders every field and form error in --color-danger (CHANGE-2)", () => {
    expect(registerForm).not.toMatch(/role="alert"[^>]*text-bronze/);
    expect(registerForm.match(/role="alert"[^>]*text-danger/g)?.length).toBeGreaterThanOrEqual(4);
  });

  it("shows the account-created states with SERVER copy verbatim", () => {
    expect(registerForm).toContain("setRegistered({ emailFailed: false, message: res.message })");
    expect(registerForm).toContain("setRegistered({ emailFailed: true, message: err.message })");
    // No invented headline may replace the server message.
    expect(registerForm).not.toContain("Check your email.");
    expect(registerForm).not.toContain("one step left");
  });

  it("gives the 503 state its own status surface plus the §5.9 resend link", () => {
    expect(registerForm).toMatch(/role="status" aria-live="polite"/);
    // The panel (status → its closing div) carries the resend link and NO
    // role="alert": the account-created states are not error surfaces.
    const statusIdx = registerForm.indexOf('role="status" aria-live="polite"');
    expect(statusIdx).toBeGreaterThan(-1);
    const panelEnd = registerForm.indexOf("</div>", statusIdx);
    expect(panelEnd).toBeGreaterThan(statusIdx);
    const panel = registerForm.slice(statusIdx, panelEnd);
    expect(panel).toContain('href="/resend-verification"');
    expect(panel).not.toContain('role="alert"');
  });

  it("keeps the live username-availability debounce wired to the hint", () => {
    expect(registerForm).toContain("usernameAvailable(trimmed)");
    expect(registerForm).toContain('id="reg-username-hint"');
    expect(registerForm).toContain("aria-live=\"polite\"");
  });

  it("takes the server's password policy as fieldErrors, never a client paraphrase", () => {
    // validate_password messages arrive as {password: [msgs]} and render per message.
    expect(registerForm).toContain("fieldErrors.password?.map");
    expect(registerPage).toContain("<RegisterForm />");
  });
});

describe("motion, tokens and scope (§5.9 / A-2 / A-3)", () => {
  it("ships no entrance motion on any auth page (§5.9: motion: none)", () => {
    for (const src of ownedSources) {
      expect(src).not.toMatch(/@starting-style|animate-\w|-enter-dur|motion-enter/);
      expect(src).not.toMatch(/\bopacity-0\b|translate-y-\[|scale-\[/);
    }
  });

  it("uses no raw hex colour in the owned auth files", () => {
    for (const src of ownedSources) {
      expect(src).not.toMatch(/#[0-9a-fA-F]{3,8}\b/);
    }
  });

  it("keeps LoginForm's two-surface contract (§5.12: the staff door stays as shipped)", () => {
    // This task redesigns /login only; the staff variant this component also
    // serves (/staff/login) must still exist, untouched, with its guard.
    expect(loginForm).toContain('variant?: "customer" | "staff"');
    expect(loginForm).toContain('variant === "staff"');
  });
});
