import { expect, test, type Page } from "@playwright/test";
import {
  API,
  CUSTOMER,
  E2ECHECK10,
  ORDER_NUMBER,
  RAZORPAY_LIVE,
  RAZORPAY_SKIP_REASON,
  STAFF,
  apiLogin,
  couponPreview,
  fetchLatestOrder,
  fetchProduct,
  formatINR,
  loginCustomer,
  type ApiOrder,
} from "./helpers";

/**
 * E2E-4 alignment. Two honesty rules bind this file:
 *
 * 1. MONEY IS DERIVED, NEVER LITERAL. The WP0 specs assumed ~₹6–7k products
 *    (₹14,000/₹11,800 orders, a ₹1,200 coupon) that drifted from the seeded
 *    catalogue (marj = ₹2,000). Every expected figure is now computed from
 *    (a) the product the test itself fetches from the API and (b) the
 *    server's own coupon-preview / checkout responses — so seed drift can
 *    never stale the spec, while a UI that renders the wrong number still
 *    fails. No assertion here is vacuous: the reconciliation below is
 *    price × qty − server discount + server shipping = server total.
 *
 * 2. THE IN-WIDGET PAYMENT IS ENV-GATED (§A-9.2 / §A-12-G8). Driving
 *    Razorpay's live test-mode checkout needs network, a working test
 *    account and backend Razorpay keys, so the payment legs live in a
 *    describe group skipped unless RAZORPAY_TEST=1 — with a visible skip
 *    reason. The pre-payment flow (coupon → ?next= login → address →
 *    review → order creation) runs unconditionally.
 */

type PaymentCall = { url: string; razorpayOrderId?: string; keyId?: string };

async function trackPaymentCalls(page: Page): Promise<PaymentCall[]> {
  const calls: PaymentCall[] = [];
  page.on("response", (res) => {
    const req = res.request();
    if (
      req.method() === "POST" &&
      (req.url().endsWith("/api/orders/payment/") ||
        req.url().endsWith("/api/orders/payment/verify/"))
    ) {
      let body: { razorpay_order_id?: string; key_id?: string } = {};
      res
        .json()
        .then((data) => {
          body = data;
        })
        .catch(() => {});
      calls.push({
        url: req.url(),
        get razorpayOrderId() {
          return body.razorpay_order_id;
        },
        get keyId() {
          return body.key_id;
        },
      });
    }
  });
  return calls;
}

async function addMarjToCart(page: Page) {
  await page.goto("/products/marj");
  await page.getByRole("button", { name: "Add to cart" }).click();
  await expect(page.getByText("Added to cart")).toBeVisible();
}

async function fillAddress(page: Page) {
  await page.goto("/checkout");
  await page.getByLabel("Full name", { exact: true }).fill("E2E Tester");
  await page.getByLabel("Email", { exact: true }).fill("e2e_customer@example.com");
  await page.getByLabel("Phone", { exact: true }).fill("9876543210");
  await page.getByLabel("Address", { exact: true }).fill("1 Test Lane");
  await page.getByLabel("City", { exact: true }).fill("Mumbai");
  await page.getByLabel("State", { exact: true }).fill("Maharashtra");
  await page.getByLabel("Pincode", { exact: true }).fill("400001");
}

/**
 * Review → order creation, capturing the checkout response — the server's
 * own order body (totals, coupon, order_number). Every money expectation
 * after this point derives from THAT response, never from a literal (E2E-4).
 */
async function createOrderViaUi(page: Page): Promise<ApiOrder> {
  const checkoutRes = page.waitForResponse(
    (res) =>
      res.url().endsWith("/api/orders/checkout/") &&
      res.request().method() === "POST",
    { timeout: 30_000 },
  );
  await page.getByTestId("create-order").click();
  const res = await checkoutRes;
  expect(res.status()).toBe(201);
  return (await res.json()) as ApiOrder;
}

/**
 * Applies the coupon through the cart UI, then reads the same server preview
 * the UI renders its numbers from. Returns the preview so downstream
 * expectations can derive from it. `merchandise` is the catalogue total the
 * test computed from the product it fetched (price × qty).
 */
async function applyCouponDerived(page: Page, merchandise: number) {
  await page.goto("/cart");
  await page.getByLabel("Coupon code").fill(E2ECHECK10);
  await page.getByRole("button", { name: "Apply" }).click();
  await expect(page.getByText(`${E2ECHECK10} applied`)).toBeVisible();

  const preview = await couponPreview(page, E2ECHECK10);
  // Non-vacuous server-truth checks: the preview's subtotal IS the catalogue
  // price the test fetched; the preview is closed arithmetic; and the coupon
  // actually discounts something — an expired or zeroed coupon renders
  // ₹0.00, the exact failure mode the E2E-1 run hit.
  expect(Number(preview.subtotal)).toBe(merchandise);
  expect(Number(preview.final_total)).toBe(
    Number(preview.subtotal) - Number(preview.discount),
  );
  expect(Number(preview.discount)).toBeGreaterThan(0);

  // The cart form must render exactly these server numbers (via the app's
  // own formatter).
  await expect(
    page.getByText(`Discount: −${formatINR(preview.discount)}`),
  ).toBeVisible();
  await expect(
    page.getByText(`Total after coupon: ${formatINR(preview.final_total)}`),
  ).toBeVisible();
  return preview;
}

/** Cart CTA → the ?next= login contract → back on /checkout, signed in. */
async function loginViaCheckoutCta(page: Page) {
  await page.getByRole("link", { name: "Sign in to checkout" }).click();
  await expect(page).toHaveURL(/\/login\?next=(%2F|\/)checkout/);
  await page.getByLabel("Username").fill(CUSTOMER.username);
  await page.getByLabel("Password").fill(CUSTOMER.password);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page).toHaveURL(/\/checkout$/);
}

/**
 * The reconciliation every created order must satisfy: merchandise (the
 * catalogue price the test fetched × qty) − the server discount + the
 * server-priced shipping equals the server total, and the customer-facing
 * identifier is an ORD-YYYY-NNNNNN order_number (§5.5).
 */
function expectOrderMoney(order: ApiOrder, merchandise: number) {
  expect(order.order_number).toMatch(ORDER_NUMBER);
  expect(Number(order.total_amount)).toBe(
    merchandise - Number(order.discount_amount) + Number(order.shipping_amount),
  );
}

/** Pays inside the real widget via a test-mode wallet. Wallet flows use the
 *  mock Success/Failure page and avoid the card-contact verification that
 *  hard-blocks the card route under automation (verified live:
 *  "The contact field is required."). RAZORPAY_TEST=1 only — the callers
 *  below sit inside the gated describe. */
async function payViaWallet(
  page: Page,
  opts: { abandonAtOtp?: boolean } = {},
) {
  const widget = page.frameLocator("iframe");
  await widget
    .getByText("Payment Options")
    .waitFor({ state: "visible", timeout: 30_000 });

  const debug = async (label: string) => {
    const snap = await widget.locator("body").ariaSnapshot().catch(() => "<none>");
    require("fs").appendFileSync(
      "e2e/artifacts/wallet-debug.txt",
      `\n===== ${label} =====\n${snap.slice(0, 2200)}\n`,
    );
  };
  require("fs").writeFileSync("e2e/artifacts/wallet-debug.txt", "");

  // Select the Wallet method. el.click() (in-page) runs the real activation
  // behavior + fires a click React's onChange responds to — dispatching a
  // bare "change" event flips the DOM without updating React state.
  const walletRadio = widget.getByRole("radio", { name: /Wallet/ });
  await walletRadio.evaluate((el) => (el as HTMLInputElement).click());
  await page.waitForTimeout(1_500);
  await debug("after wallet radio click");

  // Wallet options grid — pick the first wallet option.
  const option = widget.getByText(/mobikwik|airtelmoney|olamoney/i).first();
  await option.dispatchEvent("click");
  await page.waitForTimeout(1_000);
  await debug("after wallet option click");

  // Proceed.
  const proceed = widget
    .getByRole("button", { name: "Continue" })
    .locator("visible=true")
    .last();
  await proceed.dispatchEvent("click");
  await page.waitForTimeout(2_500);

  // Mobikwik shows its own contact sheet ("To continue with mobikwik") —
  // fill THAT textbox (the last one) and submit its Continue. Also capture
  // Razorpay API responses to see if "invalid" is a server-side rejection.
  const rzpApiResponses: string[] = [];
  page.on("response", (res) => {
    if (res.url().includes("api.razorpay.com")) {
      rzpApiResponses.push(`${res.status()} ${res.request().method()} ${res.url()}`);
    }
  });
  const walletSheet = widget.getByText(/To continue with (mobikwik|olamoney|airtel)/i);
  if (await walletSheet.first().isVisible().catch(() => false)) {
    const mobile = widget.getByRole("textbox", { name: /mobile/i }).last();
    // NOTE: the widget's validator rejects sequential numbers like
    // 9876543210 — a realistic number passes (verified live).
    await mobile.fill("8874451267");
    const sheetContinue = widget
      .getByRole("button", { name: "Continue" })
      .last();
    await sheetContinue.dispatchEvent("click");
    await page.waitForTimeout(2_500);
    const still = await walletSheet.first().isVisible().catch(() => false);
    if (still) {
      await mobile.fill("");
      await mobile.pressSequentially("8874451267", { delay: 120 });
      await sheetContinue.dispatchEvent("click");
      await page.waitForTimeout(2_500);
    }
    require("fs").writeFileSync(
      "e2e/artifacts/rzp-api-log.txt",
      `sheet still visible: ${still}\n${rzpApiResponses.join("\n")}`,
    );
    await debug("after wallet contact submit");
  }

  // OTP step (test mode accepts any code) — wait for it deterministically.
  const otpInput = widget.getByPlaceholder("Enter OTP");
  await otpInput.waitFor({ state: "visible", timeout: 20_000 }).catch(() => {});
  if (await otpInput.isVisible().catch(() => false)) {
    if (opts.abandonAtOtp) {
      // Layered exit: trusted click on the host overlay (moves focus out of
      // the widget), then Escape; then the OTP screen's Cancel.
      await page.mouse.click(15, 400);
      await page.keyboard.press("Escape");
      await page.waitForTimeout(1_500);
      if (await otpInput.isVisible().catch(() => false)) {
        const cancel = widget.getByRole("button", { name: "Cancel" }).first();
        await cancel.dispatchEvent("click");
        await page.waitForTimeout(1_500);
        if (await otpInput.isVisible().catch(() => false)) {
          await cancel
            .evaluate((el) => (el as HTMLElement).click())
            .catch(() => {});
          await page.waitForTimeout(1_500);
        }
      }
      return widget;
    }
    await otpInput.fill("123456");
    const otpContinue = widget
      .getByRole("button", { name: "Continue" })
      .last();
    await otpContinue.dispatchEvent("click");
    await page.waitForTimeout(2_500);
  }
  return widget;
}

test.describe.serial("checkout → payment (payment legs gated on RAZORPAY_TEST=1, E2E-4)", () => {
  test("PII: saved checkout details prefill on return; forget removes them", async ({ page }) => {
    const prod = await fetchProduct(page, "marj");

    await loginCustomer(page);
    await addMarjToCart(page);
    await fillAddress(page);
    await expect(page.getByTestId("pii-disclosure")).toBeVisible();
    await expect(page.getByText(/saved on this device only/i)).toBeVisible();
    await page.getByTestId("address-submit").click();

    // The details persist the moment a working order exists (CheckoutView's
    // saveShipping on order creation) — no payment needed, so this PII
    // contract runs without RAZORPAY_TEST.
    const created = await createOrderViaUi(page);
    expect(created.status).toBe("pending");
    expect(Number(created.discount_amount)).toBe(0); // no coupon in this flow
    expectOrderMoney(created, Number(prod.price));

    // Second visit: /checkout remounts at the address step with the saved
    // details prefilled, plus the visible note and the forget control.
    await page.goto("/checkout");
    await expect(
      page.getByText("Prefilled from details saved on this device."),
    ).toBeVisible();
    await expect(page.getByTestId("forget-saved-details")).toBeVisible();
    await page.screenshot({
      path: "e2e/artifacts/checkout-address-prefill-pii.png",
      fullPage: true,
    });
    await page.getByTestId("forget-saved-details").click();
    await expect(page.getByText("Saved details were removed.")).toBeVisible();
  });

  test("pre-payment: coupon → login via ?next= → address → order creation (server money)", async ({ page }) => {
    const prod = await fetchProduct(page, "marj");
    // One flacon added below — the merchandise total is the catalogue price
    // the test itself read, never a literal (E2E-4).
    const merchandise = Number(prod.price);

    // Deliberately anonymous: the session cookie comes from the browser's
    // own cart interactions, and the ?next= login contract is exercised for real.
    await addMarjToCart(page);

    // Cart: coupon preview — UI figures derived from the server preview.
    const preview = await applyCouponDerived(page, merchandise);
    await loginViaCheckoutCta(page);

    // Address form: PII disclosure visible at the point of first save.
    await expect(page.getByTestId("pii-disclosure")).toBeVisible();
    await expect(page.getByText(/saved on this device only/i)).toBeVisible();
    await fillAddress(page);

    // Review step: client subtotal (server cart prices) + coupon echo.
    await page.getByTestId("address-submit").click();
    await expect(
      page.getByText(`Subtotal ${formatINR(merchandise)} · coupon ${E2ECHECK10}`),
    ).toBeVisible();

    // Order creation — server truth reconciles against the catalogue price
    // the test fetched: price × qty − discount + shipping (E2E-4).
    const created = await createOrderViaUi(page);
    expect(created.status).toBe("pending");
    expect(created.coupon).toBe(E2ECHECK10);
    expect(Number(created.discount_amount)).toBe(Number(preview.discount));
    expectOrderMoney(created, merchandise);
    await expect(page.getByTestId("pay-button")).toHaveText(
      `Pay ${formatINR(created.total_amount)}`,
    );
  });

  // ---------------------------------------------------------------------
  // Razorpay test-mode payment legs (E2E-4 / §A-9.2 / §A-12-G8). Everything
  // in this group drives (or depends on) a REAL live-widget payment: the
  // checkout widget iframe, the payment/ + payment/verify/ round-trips,
  // stock decrement at verify, the captured-but-unfulfillable 409. Without
  // RAZORPAY_TEST=1 the whole group skips (before any browser or backend
  // work) with the visible reason below; the pre-payment tests still run.
  // ---------------------------------------------------------------------
  test.describe("payment legs (RAZORPAY_TEST=1)", () => {
    test.skip(!RAZORPAY_LIVE, RAZORPAY_SKIP_REASON);

    test("happy path: coupon → login → test-mode payment → confirmed", async ({ page }) => {
      const prod = await fetchProduct(page, "marj");
      const merchandise = Number(prod.price);
      const stockBefore = prod.stock;

      await addMarjToCart(page);
      const preview = await applyCouponDerived(page, merchandise);
      await loginViaCheckoutCta(page);
      await fillAddress(page);
      await page.getByTestId("address-submit").click();
      const created = await createOrderViaUi(page);
      expect(created.coupon).toBe(E2ECHECK10);
      expect(Number(created.discount_amount)).toBe(Number(preview.discount));
      expectOrderMoney(created, merchandise);

      // Real Razorpay test-mode payment.
      const calls = await trackPaymentCalls(page);
      await page.getByTestId("pay-button").click();
      await expect(
        page.getByText("Complete the payment in the secure window."),
      ).toBeVisible();
      await payViaWallet(page);

      // Verify → confirmation landing.
      await expect(page).toHaveURL(/\/orders\/\d+\?payment=success/, { timeout: 40_000 });
      await expect(page.getByTestId("payment-success-banner")).toBeVisible();
      await expect(page.getByText("Confirmed").first()).toBeVisible();

      // Backend truth: THIS order confirmed with the same server money the
      // pre-payment leg reconciled — derived, never a literal (E2E-4).
      const { access } = await apiLogin(page, CUSTOMER);
      const paid = await fetchLatestOrder(page, access);
      expect(paid.id).toBe(created.id);
      expect(paid.status).toBe("confirmed");
      expect(paid.total_amount).toBe(created.total_amount);
      expect(paid.discount_amount).toBe(created.discount_amount);
      expect(paid.coupon).toBe(E2ECHECK10);

      // Stock decremented exactly once; cart cleaned.
      const prodAfter = await fetchProduct(page, "marj");
      expect(prodAfter.stock).toBe(stockBefore - 1);
      const cart = await (await page.request.get(`${API}/api/cart/`)).json();
      expect(cart.items).toHaveLength(0);

      // Payment gateway used the TEST key (no real money involved).
      expect(calls.length).toBeGreaterThanOrEqual(1);
      expect(calls[0].keyId).toMatch(/^rzp_test_/);
    });

    test("abandoned payment → resume from order detail → fresh idempotent payment/ call", async ({ page }) => {
      await loginCustomer(page);
      await addMarjToCart(page);
      await fillAddress(page);

      const calls = await trackPaymentCalls(page);
      await page.getByTestId("address-submit").click();
      const created = await createOrderViaUi(page);
      expect(created.status).toBe("pending");
      await page.getByTestId("pay-button").click();
      // Widget is open at the contact/OTP stage — the user navigates away
      // (refresh/navigation destroys the widget; the order stays pending).
      await page
        .getByText(/Enter mobile/i)
        .first()
        .waitFor({ state: "visible", timeout: 30_000 })
        .catch(() => {});
      await page.goto("/orders");

      // Resume: open the pending order by its customer-facing reference —
      // §5.5 renders `Order #{order_number}` (ORD-YYYY-NNNNNN), not the
      // sequential id, so the old `/Order #\d+/` can never match that copy.
      await page
        .getByRole("link", { name: `Order #${created.order_number}` })
        .first()
        .click();
      await expect(page.getByTestId("resume-payment-panel")).toBeVisible();
      await page.getByTestId("resume-payment").click();
      await expect(
        page.getByText("Complete the payment in the secure window."),
      ).toBeVisible();
      await payViaWallet(page);

      // Two payment/ calls for the SAME order — same razorpay_order_id.
      const paymentCalls = calls.filter((c) =>
        c.url.endsWith("/api/orders/payment/"),
      );
      expect(paymentCalls.length).toBe(2);
      expect(paymentCalls[1].razorpayOrderId).toBe(paymentCalls[0].razorpayOrderId);
      expect(paymentCalls[0].razorpayOrderId).toBeTruthy();

      // The resumed session completes → confirmed.
      await expect(page).toHaveURL(/\/orders\/\d+\?payment=success/, { timeout: 40_000 });
      await expect(page.getByTestId("payment-success-banner")).toBeVisible();
    });

    test("409 captured-but-unfulfillable → dead end, no retry path exists", async ({ page }) => {
      const prod = await fetchProduct(page, "marj");
      const originalStock = prod.stock;

      await loginCustomer(page);
      await addMarjToCart(page);
      await fillAddress(page);

      const calls = await trackPaymentCalls(page);
      await page.getByTestId("address-submit").click();
      const created = await createOrderViaUi(page);
      expect(created.status).toBe("pending");

      // Force the oversell condition between order creation and payment:
      // staff zeroes the stock while the customer's order is pending.
      //
      // KNOWN ASPIRATIONAL SEAM (E2E-4 residual, documented not hidden):
      // since SPEC-6-02 `stock` is read-only on REST product updates — this
      // PATCH is accepted but does not mutate stock — so the 409 cannot
      // trigger until the backend exposes an e2e-reachable inventory adjust
      // (logged in BACKEND_REQUESTS.md). The test stays gated on
      // RAZORPAY_TEST=1 and fails loudly in a full run rather than faking a
      // green it cannot prove.
      const staff = await apiLogin(page, STAFF, "staff");
      await page.request.patch(`${API}/api/products/marj/`, {
        headers: { Authorization: `Bearer ${staff.access}` },
        data: { stock: 0 },
      });

      await page.getByTestId("pay-button").click();
      await payViaWallet(page);

      // Money captured, stock unavailable → 409 → the dead end.
      await expect(page.getByTestId("captured-dead-end")).toBeVisible({ timeout: 40_000 });
      await expect(page.getByText(/Payment received — order not completed\./)).toBeVisible();
      await expect(page.getByText(/contact support/i)).toBeVisible();
      await expect(page.getByText(/Do not retry this payment/)).toBeVisible();

      // Structural dead end: no retry affordance, and no further gateway calls.
      await expect(page.getByTestId("retry-payment")).toHaveCount(0);
      // Exactly one payment/ and one verify/ (the 409 one) — zero retries.
      const paymentCalls = calls.filter((c) => c.url.endsWith("/api/orders/payment/"));
      const verifyCalls = calls.filter((c) => c.url.endsWith("/api/orders/payment/verify/"));
      expect(paymentCalls.length).toBe(1);
      expect(verifyCalls.length).toBe(1);
      await page.waitForTimeout(3_000);
      expect(calls.length).toBe(2);

      // Backend truth: order still pending, stock NOT decremented by verify.
      const { access } = await apiLogin(page, CUSTOMER);
      const latest = await fetchLatestOrder(page, access);
      expect(latest.id).toBe(created.id);
      expect(latest.status).toBe("pending");
      const prodAfter = await fetchProduct(page, "marj");
      expect(prodAfter.stock).toBe(0);

      // Restore stock for cleanup (the test-mode capture is not undone — V-03).
      await page.request.patch(`${API}/api/products/marj/`, {
        headers: { Authorization: `Bearer ${staff.access}` },
        data: { stock: originalStock },
      });

      await page.screenshot({
        path: "e2e/artifacts/checkout-409-dead-end.png",
        fullPage: true,
      });
    });
  });
});
