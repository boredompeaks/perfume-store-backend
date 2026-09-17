import { expect, type Page } from "@playwright/test";

export const API = "http://localhost:8000";
export const CUSTOMER = { username: "e2e_customer", password: "e2e-Customer-9" };

export async function apiLogin(
  page: Page,
  creds: { username: string; password: string },
) {
  const res = await page.request.post(`${API}/api/accounts/login/`, {
    data: creds,
  });
  expect(res.ok()).toBeTruthy();
  return (await res.json()) as { access: string; refresh: string };
}

/**
 * Establishes the Django session cookie (shared cookie jar) + JWT so the
 * app boots fully authenticated. The API origin must be same-site with the
 * frontend origin — see PLAN.md §1 (127.0.0.1 vs localhost breaks cookies).
 */
export async function loginCustomer(page: Page) {
  await page.request.get(`${API}/api/cart/`);
  const { refresh } = await apiLogin(page, CUSTOMER);
  await page.addInitScript(({ token }) => {
    localStorage.setItem("aurel.refresh", token);
  }, { token: refresh });
}
