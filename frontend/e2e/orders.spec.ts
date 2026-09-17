import { expect, test, type Page } from "@playwright/test";
import { apiLogin, loginCustomer } from "./helpers";

const API = "http://localhost:8000";
const CUSTOMER = { username: "e2e_customer", password: "e2e-Customer-9" };

test("orders list and detail render; pending order shows resume panel", async ({
  page,
}) => {
  await loginCustomer(page);

  // Seed an order through the real API (session cart + JWT checkout).
  await page.request.post(`${API}/api/cart/`, {
    data: { product_id: 4, quantity: 2 },
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
    total_amount: string;
    status: string;
  };
  expect(order.status).toBe("pending");

  // List: row with id, item summary, server total.
  await page.goto("/orders");
  const row = page.getByRole("link", { name: new RegExp(`Order #${order.id}`) });
  await expect(row).toBeVisible();
  await expect(row.getByText("marj × 2")).toBeVisible();
  await expect(row.getByText("₹4,000.00")).toBeVisible();
  await expect(row.getByText("Pending payment")).toBeVisible();

  // Detail: items, server total, delivery block, resume panel.
  await row.click();
  await expect(page.getByRole("heading", { name: `Order #${order.id}` })).toBeVisible();
  await expect(page.getByTestId("resume-payment-panel")).toBeVisible();
  await expect(page.getByText("marj × 2")).toBeVisible();
  await expect(page.getByText("₹4,000.00").first()).toBeVisible();
  await expect(page.getByText("Mumbai, Maharashtra 400001")).toBeVisible();
});

test("anonymous /orders shows the sign-in gate", async ({ page }) => {
  await page.goto("/orders");
  await expect(page.getByText("Sign in to see your orders.")).toBeVisible();
});
