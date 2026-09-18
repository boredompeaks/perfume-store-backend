import { expect, test, type Page } from "@playwright/test";

const API = "http://localhost:8000";
const CUSTOMER = { username: "e2e_customer", password: "e2e-Customer-9" };
const STAFF = { username: "e2e_staff", password: "e2e-Staff-9" };

type PaymentCall = { url: string; razorpayOrderId?: string; keyId?: string };

async function apiLogin(page: Page, creds: { username: string; password: string }) {
  const res = await page.request.post(`${API}/api/accounts/login/`, {
    data: creds,
  });
  expect(res.ok()).toBeTruthy();
  return (await res.json()) as { access: string; refresh: string };
}

/** Establishes the Django session cookie + JWT so the app is fully authed. */
async function loginCustomer(page: Page) {
  await page.request.get(`${API}/api/cart/`);
  const { refresh } = await apiLogin(page, CUSTOMER);
  await page.addInitScript(({ token }) => {
    localStorage.setItem("aurel.refresh", token);
  }, { token: refresh });
}

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

async function createOrderAndOpenPay(page: Page) {
  await page.getByTestId("address-submit").click();
  await page.getByTestId("create-order").click();
  await page.getByTestId("pay-button").click();
}

/** Waits for the Razorpay widget and clears the contact sheet if shown.
 *  All interaction is focus/keyboard-based: the widget's stack overlay
 *  intercepts pointer events nondeterministically, but focus() bypasses it. */
async function openWidgetReady(page: Page) {
  const widget = page.frameLocator("iframe");
  await widget
    .getByText("Payment Options")
    .waitFor({ state: "visible", timeout: 30_000 });

  // Contact sheet — left alone: it doesn't block the card form (its Continue
  // stays enabled; verified live).Attempting to satisfy it re-activates its
  // blocking overlay.
  return widget;
}

/** Pays inside the real widget via a test-mode wallet. Wallet flows use the
 *  mock Success/Failure page and avoid the card-contact verification that
 *  hard-blocks the card route under automation (verified live:
 *  "The contact field is required."). */
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

test.describe.serial("checkout → payment (Razorpay test mode)", () => {
  let stockBefore: number;

  test("happy path: coupon → login via ?next= → order → test-mode payment → confirmed", async ({ page }) => {
    const prod = await (await page.request.get(`${API}/api/products/marj/`)).json();
    stockBefore = Number(prod.stock);

    // Deliberately anonymous: the session cookie comes from the browser's
    // own cart interactions, and the ?next= login contract is exercised for real.
    await addMarjToCart(page);

    // Cart: coupon preview (server truth).
    await page.goto("/cart");
    await page.getByLabel("Coupon code").fill("E2ECHECK10");
    await page.getByRole("button", { name: "Apply" }).click();
    await expect(page.getByText("E2ECHECK10 applied")).toBeVisible();
    await expect(page.getByText(/Discount: −₹200\.00/)).toBeVisible();
    await expect(page.getByText(/Total after coupon: ₹1,800\.00/)).toBeVisible();

    // Cart CTA → login → must land back on /checkout (the ?next= contract).
    await page.getByRole("link", { name: "Sign in to checkout" }).click();
    await expect(page).toHaveURL(/\/login\?next=(%2F|\/)checkout/);
    await page.getByLabel("Username").fill(CUSTOMER.username);
    await page.getByLabel("Password").fill(CUSTOMER.password);
    await page.getByRole("button", { name: "Sign in" }).click();
    await expect(page).toHaveURL(/\/checkout$/);

    // Address form: PII disclosure visible at the point of first save.
    await expect(page.getByTestId("pii-disclosure")).toBeVisible();
    await expect(page.getByText(/saved on this device only/i)).toBeVisible();
    await fillAddress(page);

    // Review → order creation (server truth total).
    await page.getByTestId("address-submit").click();
    await expect(page.getByText(/Subtotal ₹2,000\.00/)).toBeVisible();
    await expect(page.getByText(/coupon E2ECHECK10/)).toBeVisible();
    await page.getByTestId("create-order").click();
    await expect(page.getByTestId("pay-button")).toHaveText(/Pay ₹1,800\.00/);

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

    // Backend truth: order confirmed with server-computed money.
    const { access } = await apiLogin(page, CUSTOMER);
    const ordersRes = await page.request.get(`${API}/api/orders/`, {
      headers: { Authorization: `Bearer ${access}` },
    });
    const orders = (await ordersRes.json()) as {
      id: number;
      status: string;
      total_amount: string;
      discount_amount: string;
      coupon: string | null;
    }[];
    const order = orders[0];
    expect(order.status).toBe("confirmed");
    expect(order.total_amount).toBe("1800.00");
    expect(order.discount_amount).toBe("200.00");
    expect(order.coupon).toBe("E2ECHECK10");

    // Stock decremented exactly once; cart cleaned.
    const prodAfter = await (await page.request.get(`${API}/api/products/marj/`)).json();
    expect(Number(prodAfter.stock)).toBe(stockBefore - 1);
    const cart = await (await page.request.get(`${API}/api/cart/`)).json();
    expect(cart.items).toHaveLength(0);

    // Payment gateway used the TEST key (no real money involved).
    expect(calls.length).toBeGreaterThanOrEqual(1);
    expect(calls[0].keyId).toMatch(/^rzp_test_/);

    // PII: second visit must prefill with the visible note + forget control.
    // The cart was emptied by the successful payment, so add an item again
    // to reach the address form.
    await addMarjToCart(page);
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

  test("abandoned payment → resume from order detail → fresh idempotent payment/ call", async ({ page }) => {
    await loginCustomer(page);
    await addMarjToCart(page);
    await fillAddress(page);

    const calls = await trackPaymentCalls(page);
    await page.getByTestId("address-submit").click();
    await page.getByTestId("create-order").click();
    await page.getByTestId("pay-button").click();
    // Widget is open at the contact/OTP stage — the user navigates away
    // (refresh/navigation destroys the widget; the order stays pending).
    await page
      .getByText(/Enter mobile/i)
      .first()
      .waitFor({ state: "visible", timeout: 30_000 })
      .catch(() => {});
    await page.goto("/orders");

    // Resume: open the pending order and complete the payment there.
    await page.getByRole("link", { name: /Order #\d+/ }).first().click();
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
    const prod = await (await page.request.get(`${API}/api/products/marj/`)).json();
    const originalStock = Number(prod.stock);

    await loginCustomer(page);
    await addMarjToCart(page);
    await fillAddress(page);

    const calls = await trackPaymentCalls(page);
    await page.getByTestId("address-submit").click();
    await page.getByTestId("create-order").click();

    // Force the oversell condition between order creation and payment:
    // staff zeroes the stock while the customer's order is pending.
    const staff = await apiLogin(page, STAFF);
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

    // Backend truth: order still pending, stock NOT decremented.
    const { access } = await apiLogin(page, CUSTOMER);
    const orders = (await (
      await page.request.get(`${API}/api/orders/`, {
        headers: { Authorization: `Bearer ${access}` },
      })
    ).json()) as { status: string }[];
    expect(orders[0].status).toBe("pending");
    const prodAfter = await (await page.request.get(`${API}/api/products/marj/`)).json();
    expect(Number(prodAfter.stock)).toBe(0);

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
