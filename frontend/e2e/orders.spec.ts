import { expect, test } from "@playwright/test";
import {
  API,
  CUSTOMER,
  ORDER_NUMBER,
  apiLogin,
  fetchProduct,
  formatINR,
  loginCustomer,
} from "./helpers";

test("orders list and detail render; pending order shows resume panel", async ({
  page,
}) => {
  await loginCustomer(page);

  // Fixture derived from the catalogue the test itself reads — no hardcoded
  // product id, name or price (E2E-4: seed drift used to stale the ₹
  // literals the WP0 spec hardcoded).
  const prod = await fetchProduct(page, "marj");
  const qty = 2;
  const merchandise = Number(prod.price) * qty;

  // Seed an order through the real API (session cart + JWT checkout).
  await page.request.post(`${API}/api/cart/`, {
    data: { product_id: prod.id, quantity: qty },
  });
  const { access } = await apiLogin(page, CUSTOMER);
  const res = await page.request.post(`${API}/api/orders/checkout/`, {
    headers: { Authorization: `Bearer ${access}` },
    data: {
      full_name: "E2E Tester",
      phone: "9876543210",
      address: "1 Test Lane",
      city: "Mumbai",
      state: "Maharashtra",
      pincode: "400001",
    },
  });
  expect(res.status()).toBe(201);
  const order = (await res.json()) as {
    id: number;
    order_number: string;
    total_amount: string;
    discount_amount: string;
    shipping_amount: string;
    status: string;
  };
  expect(order.status).toBe("pending");
  expect(order.order_number).toMatch(ORDER_NUMBER);
  // Server money math, derived not literal: catalogue price × qty, no coupon
  // in this flow, plus whatever the server priced shipping at (E2E-4).
  expect(Number(order.discount_amount)).toBe(0);
  expect(Number(order.total_amount)).toBe(
    merchandise + Number(order.shipping_amount),
  );

  // List: row with order_number (the customer-facing reference — §5.5
  // replaces the sequential id in all customer-facing text), the item
  // summary built from the fetched product, and the server total as the UI
  // formats it with the app's own formatter.
  await page.goto("/orders");
  const row = page.getByRole("link", {
    name: `Order #${order.order_number}`,
  });
  await expect(row).toBeVisible();
  await expect(row.getByText(`${prod.name} × ${qty}`)).toBeVisible();
  await expect(row.getByText(formatINR(order.total_amount))).toBeVisible();
  await expect(row.getByText("Pending payment")).toBeVisible();

  // Detail: items, server total, delivery block, resume panel.
  await row.click();
  await expect(
    page.getByRole("heading", { name: `Order ${order.order_number}` }),
  ).toBeVisible();
  await expect(page.getByTestId("resume-payment-panel")).toBeVisible();
  await expect(page.getByText(`${prod.name} × ${qty}`)).toBeVisible();
  await expect(page.getByText(formatINR(order.total_amount)).first()).toBeVisible();
  await expect(page.getByText("Mumbai, Maharashtra 400001")).toBeVisible();
});

test("anonymous /orders shows the sign-in gate", async ({ page }) => {
  await page.goto("/orders");
  await expect(page.getByText("Sign in to see your orders.")).toBeVisible();
});
