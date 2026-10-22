import { readFileSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import { ApiError } from "@/lib/api";
import {
  activeFraction,
  checkoutGate,
  cheapestOption,
  estimateErrorCopy,
  newAttemptKey,
  stepFraction,
  type EstimateOption,
} from "./CheckoutView";

const here = fileURLToPath(new URL(".", import.meta.url));
const viewSource = readFileSync(join(here, "CheckoutView.tsx"), "utf8");
const addressSource = readFileSync(join(here, "AddressForm.tsx"), "utf8");

/**
 * The flag-gated guest wall (spec CHANGE-11, backend TIER1-GUEST-DEADEND).
 * This branch is an audited contract, not a UI preference: flag OFF must be
 * the pre-guest sign-in wall verbatim, flag ON must hand the decision to the
 * server — the client mints nothing either way.
 */
describe("guest wall flag gate", () => {
  it("walls an anonymous visitor when guestCheckout is off", () => {
    expect(checkoutGate("anonymous", false)).toBe("wall");
  });

  it("lets an anonymous visitor reach review when guestCheckout is on", () => {
    // The 403 refusal (and its verbatim message) happens at submit — the gate
    // itself never pre-judges, and never fabricates a session.
    expect(checkoutGate("anonymous", true)).toBe("flow");
  });

  it("never walls a signed-in customer, with the flag on or off", () => {
    expect(checkoutGate("authenticated", false)).toBe("flow");
    expect(checkoutGate("authenticated", true)).toBe("flow");
  });

  it("holds the boot state regardless of the flag", () => {
    expect(checkoutGate("loading", false)).toBe("loading");
    expect(checkoutGate("loading", true)).toBe("loading");
  });
});

/**
 * 5.4's step indicator: fill = stepsCompleted / 2. Three steps, so 0, half, 1.
 */
describe("step indicator fraction", () => {
  it("is empty on Delivery, half on Review, full on Payment", () => {
    expect(stepFraction("details")).toBe(0);
    expect(stepFraction("review")).toBe(0.5);
    expect(stepFraction("pay")).toBe(1);
  });
});

/**
 * 5.4: the estimate's 400 shows the refusal WITH the backend's own message
 * verbatim; only a transport failure gets local copy.
 */
describe("delivery estimate error copy", () => {
  it("keeps the backend's 400 message verbatim behind the spec line", () => {
    const server =
      "Shipping is not available for this destination. Please check your delivery address.";
    expect(estimateErrorCopy(new ApiError(400, server))).toBe(
      `We cannot deliver there yet. ${server}`,
    );
  });

  it("renders any other ApiError exactly as the server said it", () => {
    expect(estimateErrorCopy(new ApiError(503, "Service unavailable"))).toBe(
      "Service unavailable",
    );
  });

  it("uses local copy only when the request never reached the server", () => {
    expect(estimateErrorCopy(new TypeError("fetch failed"))).toBe(
      "We could not fetch delivery options. Check your connection and try again.",
    );
  });
});

/** 5.4: the cheapest option is preselected — selection over server quotes. */
describe("cheapest option selection", () => {
  const standard: EstimateOption = {
    method: "standard",
    name: "Standard",
    amount: "70.00",
    free_shipping: false,
  };
  const express: EstimateOption = {
    method: "express",
    name: "Express",
    amount: "150.00",
    free_shipping: false,
  };
  const free: EstimateOption = {
    method: "free",
    name: "Free shipping",
    amount: "0.00",
    free_shipping: true,
  };

  it("preselects the lowest server-quoted amount", () => {
    expect(cheapestOption([express, standard, free])?.method).toBe("free");
    expect(cheapestOption([express, standard])?.method).toBe("standard");
  });

  it("answers nothing for an empty quote list", () => {
    expect(cheapestOption([])).toBeNull();
  });
});

/** 5.4 step 2: one key per attempt, stable across retries, server cap 128. */
describe("order-attempt idempotency key", () => {
  it("stays within the backend's 128-character cap and differs per attempt", () => {
    const first = newAttemptKey();
    const second = newAttemptKey();
    expect(first.length).toBeLessThanOrEqual(128);
    expect(first.length).toBeGreaterThan(0);
    expect(second).not.toBe(first);
  });
});

/**
 * Source-level preservation pins: the behaviours the task lists as
 * non-negotiable are asserted where they live, so a refactor that drops a
 * hook or a token fails here rather than in review.
 */
describe("checkout preservation pins (5.4)", () => {
  it("keeps the guest hand-off, wall and order hooks by testid", () => {
    for (const id of [
      "guest-pay-handoff",
      "checkout-signin",
      "create-order",
      "delivery-estimate",
    ]) {
      expect(viewSource).toContain(`data-testid="${id}"`);
    }
  });

  it("mints and stores no credential in the view", () => {
    // No faked JWT, no client-side token store — the refusal path must stay
    // a sign-in hand-off (TIER1-GUEST-DEADEND).
    expect(viewSource).not.toContain("localStorage");
    expect(viewSource).not.toContain("sessionStorage");
    expect(viewSource).not.toContain("btoa(");
  });

  it("names the step motion with the progress-intent tokens only", () => {
    expect(viewSource).toContain(
      "transform var(--motion-progress-dur) var(--motion-ease)",
    );
    // No raw millisecond literal anywhere in the page's motion.
    expect(viewSource).not.toMatch(/\b\d+ms\b/);
  });

  it("keeps the PII disclosure block intact in AddressForm", () => {
    expect(addressSource).toContain(
      "Prefilled from details saved on this device.",
    );
    expect(addressSource).toContain("Forget saved details");
    expect(addressSource).toContain('data-testid="pii-disclosure"');
    expect(addressSource).toContain('data-testid="forget-saved-details"');
    expect(addressSource).toContain('data-testid="address-submit"');
  });

  it("keeps the load-bearing 404 copy on the create-order path", () => {
    expect(viewSource).toContain(
      "Your cart session expired. Please add items again.",
    );
  });
});

/**
 * Owner directive D2 (U-W10-T2): an unauthenticated visitor who reaches
 * /checkout is immediately replaced with /register?next=/checkout — not a
 * dead-end wall, not the guest flow. Source-level, like the rest of this
 * file (vitest runs in the node environment). Every half of that sentence is
 * load-bearing and separately pinned: the TARGET is exact, `replace` is what
 * keeps Back from bounce-landing on checkout, and the guard is what keeps a
 * state update (or StrictMode's double-invoke) from looping the redirect.
 */
describe("D2 anonymous redirect (U-W10-T2)", () => {
  // The redirect effect, located structurally (guard ref → effect → replace
  // → deps) so no comment quoting the target can make a pin pass.
  const redirectBody = () => {
    const guardAt = viewSource.indexOf("const redirected = useRef(false);");
    const effectAt = viewSource.indexOf("useEffect(() => {", guardAt);
    const replaceAt = viewSource.indexOf("router.replace(", effectAt);
    const depsAt = viewSource.indexOf("}, [authStatus, router]);", replaceAt);
    return { guardAt, effectAt, replaceAt, depsAt };
  };

  it("replaces to the exact target — replace, never push", () => {
    const { guardAt, effectAt, replaceAt, depsAt } = redirectBody();
    expect(guardAt).toBeGreaterThan(-1);
    expect(effectAt).toBeGreaterThan(guardAt);
    expect(replaceAt).toBeGreaterThan(effectAt);
    expect(depsAt).toBeGreaterThan(replaceAt);
    expect(
      viewSource.slice(replaceAt, depsAt),
    ).toContain('router.replace("/register?next=/checkout")');
    // `push` would leave /checkout in the history, so Back from /register
    // would land straight back on the route that just redirected.
    expect(viewSource).not.toContain("router.push(");
  });

  it("fires once per mount, on a resolved anonymous signal only", () => {
    const { effectAt, replaceAt, depsAt } = redirectBody();
    expect(viewSource).toContain(
      'if (authStatus !== "anonymous" || redirected.current) return;',
    );
    expect(viewSource).toContain("redirected.current = true;");
    // The decision reads the auth context this component already has — the
    // effect body itself must carry no request of any kind (no probe
    // endpoint, no credential invented to decide with).
    const effectBody = viewSource.slice(effectAt, depsAt);
    expect(effectBody).not.toMatch(/fetch|apiFetch|queryKey|axios/);
    expect(replaceAt).toBeGreaterThan(effectAt);
  });

  it("falls through to the existing loading state while auth is undetermined", () => {
    // "loading" is not "anonymous": no signal, no guess, no redirect — the
    // page keeps today's loading surface instead of looping or walling.
    const anonymousReturn = viewSource.indexOf(
      'if (gate === "loading" || authStatus === "anonymous" || cartLoading)',
    );
    expect(anonymousReturn).toBeGreaterThan(-1);
    // And it returns BEFORE the wall branch can render: an anonymous view is
    // the redirect in flight — neither the dead end nor the guest flow paints
    // for a route the customer is already leaving.
    expect(anonymousReturn).toBeLessThan(
      viewSource.indexOf('if (gate === "wall")'),
    );
  });

  it("keeps the flag-off wall contract intact behind the redirect", () => {
    // checkoutGate is not rewritten by D2 — the pin below is the contract the
    // wall branch still answers, even though the redirect now precedes it.
    expect(checkoutGate("anonymous", false)).toBe("wall");
    expect(checkoutGate("anonymous", true)).toBe("flow");
  });
});

/**
 * D2's round-trip, both sides. /checkout hands over `?next=/checkout`; both
 * auth doors must take it through `safeNext` (the W8 source-verified chain),
 * and the cart's sign-in escape must still point at the login door carrying
 * the same target. The register door reads the param into `GuestOnly`, so an
 * already-signed-in visitor is replaced straight back onto checkout.
 */
describe("the next=/checkout chain (D2, U-W10-T2)", () => {
  const read = (rel: string) => readFileSync(join(here, rel), "utf8");
  const registerPage = read(join("../../app/register", "page.tsx"));
  const loginPage = read(join("../../app/login", "page.tsx"));
  const loginForm = read(join("../auth", "LoginForm.tsx"));
  const cartSource = read(join("../cart", "CartView.tsx"));

  it("/register takes ?next= through safeNext into GuestOnly", () => {
    expect(registerPage).toContain("safeNext(");
    expect(registerPage).toContain("next={next}");
    expect(registerPage).toContain("<RegisterForm />");
  });

  it("/login still takes ?next= and pushes it after a successful sign-in", () => {
    expect(loginPage).toContain("safeNext(");
    expect(loginPage).toContain("next={next}");
    expect(loginForm).toContain('router.push(next ?? "/")');
  });

  it("the cart's sign-in escape still carries the checkout target", () => {
    expect(cartSource).toContain('href="/login?next=/checkout"');
  });
});

/**
 * D1 screen (U-W12-T8), media/designs/maison_ther_commande_r_glement: the
 * checkout restyle's load-bearing halves. The screen drives the visuals; these
 * pins hold what it must not silently drop — the fill arithmetic that reaches
 * the pixels (active tab's third of the rule, not a redefinition of the pinned
 * stepFraction), the open-hairline field treatment in the control-boundary
 * token, and the eyebrow+serif-heading pairs §7.2 moves focus between.
 */
describe("D1 checkout screen restyle (U-W12-T8)", () => {
  it("fills the rule from the active tab's fraction, derived from stepFraction", () => {
    expect(activeFraction("details")).toBeCloseTo(1 / 3, 10);
    expect(activeFraction("review")).toBeCloseTo(2 / 3, 10);
    expect(activeFraction("pay")).toBe(1);
    // That fraction is what scaleX is fed, so this pin drives the pixels.
    expect(viewSource).toContain("scaleX(${activeFraction(step)})");
    // The 5.4 arithmetic itself is untouched: 0 / 0.5 / 1 still.
    expect(stepFraction("details")).toBe(0);
    expect(stepFraction("review")).toBe(0.5);
    expect(stepFraction("pay")).toBe(1);
  });

  it("keeps the address fields open-hairline in the control-boundary token", () => {
    // Bottom rule only, and the rule uses --color-field-border (3.90:1) —
    // never the decorative hairline, and never back into the boxed inputClass.
    expect(addressSource).toContain("border-b border-field-border");
    expect(addressSource).toContain("bg-transparent");
    expect(addressSource).not.toContain(
      "border border-field-border bg-surface",
    );
    expect(addressSource).not.toContain("inputClass");
    // Label above the field, in the small tracked eyebrow step.
    expect(addressSource).toContain(
      'className="mb-1 block text-eyebrow text-bronze"',
    );
  });

  it("pairs each screen eyebrow with the focused step heading", () => {
    for (const [eyebrow, heading] of [
      ["Lieu de Réception", "Carriage Destination"],
      ["Inventaire Raisonné", "Order Verification"],
      ["Règlement Scellé", "Payment Authorization"],
    ]) {
      expect(viewSource).toContain(
        `<p className="text-eyebrow text-bronze">${eyebrow}</p>`,
      );
      expect(viewSource).toContain(`\n              ${heading}\n`);
    }
    // The summary rail the screen shows, and its provider note's processor.
    expect(viewSource).toContain("Processed securely via Razorpay.");
    expect(viewSource).not.toContain("Stripe");
  });
});
