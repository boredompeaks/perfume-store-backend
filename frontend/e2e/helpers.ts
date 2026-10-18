import { expect, type Page } from "@playwright/test";
import { formatINR } from "../src/lib/money";

// The app's OWN formatter (src/lib/money.ts), re-exported so an e2e text
// assertion can never drift from what the UI actually renders.
export { formatINR };

export const API = "http://localhost:8000";
export const CUSTOMER = { username: "e2e_customer", password: "e2e-Customer-9" };
export const STAFF = { username: "e2e_staff", password: "e2e-Staff-9" };

/**
 * Coupon used by the checkout happy path. Its CONFIG lives in the e2e dev
 * database (window maintained by the E2E-1 seed pass), NOT in this repo — so
 * no spec may hardcode what it discounts. Every expected discount is DERIVED
 * from the server's own preview response (E2E-4).
 */
export const E2ECHECK10 = "E2ECHECK10";

/**
 * E2E-4 gate: the payment legs below drive Razorpay's LIVE test-mode
 * checkout widget — network, a working Razorpay test account and backend
 * Razorpay keys required (§A-9.2 / §A-12-G8). RAZORPAY_TEST=1 opts them in;
 * without it they skip as a group with a visible reason while the
 * pre-payment suite (coupon → login → address → review → order creation)
 * still runs against the backend.
 */
export const RAZORPAY_LIVE = process.env.RAZORPAY_TEST === "1";
export const RAZORPAY_SKIP_REASON =
  "E2E-4: in-widget Razorpay payment needs live test mode (set RAZORPAY_TEST=1); the pre-payment flow still runs";

/** The customer-facing order reference (§5.5): ORD-YYYY-NNNNNN. */
export const ORDER_NUMBER = /^ORD-\d{4}-\d{6}$/;

/** The subset of OrderSerializer these specs reconcile against. */
export type ApiOrder = {
  id: number;
  order_number: string;
  status: string;
  coupon: string | null;
  discount_amount: string;
  total_amount: string;
  shipping_amount: string;
};

/** The public product payload the specs read their fixture from. */
export type ApiProduct = {
  id: number;
  name: string;
  slug: string;
  price: string;
  stock: number;
};

/** POST /api/orders/apply-coupon/ — the numbers the cart form renders. */
export type CouponPreviewBody = {
  coupon: string;
  subtotal: string;
  discount: string;
  final_total: string;
};

/**
 * JWT login through the account's own door (SPEC-20-10): customers
 * authenticate via the storefront door (no MFA field, refuses privileged
 * accounts), staff via the MFA-aware staff door. The refresh token is an
 * HttpOnly cookie (R-17.12) and never appears in the response body.
 */
export async function apiLogin(
  page: Page,
  creds: { username: string; password: string },
  surface: "customer" | "staff" = "customer",
) {
  const path =
    surface === "staff"
      ? `${API}/api/accounts/login/`
      : `${API}/api/accounts/storefront/login/`;
  const res = await page.request.post(path, { data: creds });
  expect(res.ok()).toBeTruthy();
  return (await res.json()) as { access: string };
}

/**
 * Establishes the session the way a browser does (§A-9.2 items 1-2): the
 * customer login POST lands the HttpOnly refresh cookie in the shared
 * cookie jar, then the NON-SECRET `aurel.session` hint (a boolean — never a
 * token; R-17.12) lets AuthProvider's boot refresh probe raise the UI into
 * its authenticated state. `page.request` and the page share one jar, so
 * subsequent browser fetches ride the same session.
 *
 * The API origin MUST be same-site with the frontend origin (PLAN.md §1):
 * run the storefront with NEXT_PUBLIC_API_BASE_URL=http://localhost:8000 or
 * Lax cookies will not cross the localhost ↔ 127.0.0.1 line.
 */
export async function loginCustomer(page: Page) {
  // Session cookie for the cart — the same jar the browser will use.
  await page.request.get(`${API}/api/cart/`);
  await apiLogin(page, CUSTOMER);
  await page.addInitScript(() => {
    try {
      localStorage.setItem("aurel.session", "1");
    } catch {
      // Opaque origin (about:blank) — the next real origin gets the hint.
    }
  });
}

/** Catalogue fixture, read by the test itself (E2E-4: never hardcode). */
export async function fetchProduct(
  page: Page,
  slug: string,
): Promise<ApiProduct> {
  const res = await page.request.get(`${API}/api/products/${slug}/`);
  expect(res.ok()).toBeTruthy();
  return (await res.json()) as ApiProduct;
}

/**
 * Server-truth coupon preview — the same endpoint the cart form renders its
 * Discount / Total-after-coupon lines from. Session-cookie scoped, no JWT.
 */
export async function couponPreview(
  page: Page,
  code: string,
): Promise<CouponPreviewBody> {
  const res = await page.request.post(`${API}/api/orders/apply-coupon/`, {
    data: { code },
  });
  expect(res.ok()).toBeTruthy();
  return (await res.json()) as CouponPreviewBody;
}

/**
 * Newest order for the JWT caller. GET /api/orders/ answers the house
 * page-number envelope ({count, total_pages, results, ...}) — never a bare
 * array — and orders are newest-first.
 */
export async function fetchLatestOrder(
  page: Page,
  access: string,
): Promise<ApiOrder> {
  const res = await page.request.get(`${API}/api/orders/`, {
    headers: { Authorization: `Bearer ${access}` },
  });
  expect(res.ok()).toBeTruthy();
  const body = (await res.json()) as { results: ApiOrder[] };
  expect(Array.isArray(body.results)).toBeTruthy();
  expect(body.results.length).toBeGreaterThan(0);
  return body.results[0];
}
