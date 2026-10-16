import { readFileSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import { ApiError } from "@/lib/api";
import {
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
