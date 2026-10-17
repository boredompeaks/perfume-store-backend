import { apiFetch } from "./api";
import type { Product, ProductPage, ProductNotes } from "./types";

export function fetchProductsPage(
  params: URLSearchParams,
): Promise<ProductPage> {
  return apiFetch<ProductPage>(`/api/products/?${params.toString()}`, {
    revalidate: 60,
  });
}

export function fetchProduct(slug: string): Promise<Product> {
  return apiFetch<Product>(`/api/products/${slug}/`, { revalidate: 60 });
}

/** One display tier of the olfactory pyramid (Top / Heart / Base). */
export type PyramidTier = {
  key: "top" | "heart" | "base";
  /** Wide-tracked indicator, e.g. "N° 01" (design system: 11px uppercase). */
  index: string;
  label: string;
  notes: NonNullable<ProductNotes["top"]>;
};

const PYRAMID_TIERS: ReadonlyArray<{
  key: PyramidTier["key"];
  index: string;
  label: string;
}> = [
  { key: "top", index: "N° 01", label: "Top" },
  { key: "heart", index: "N° 02", label: "Heart" },
  { key: "base", index: "N° 03", label: "Base" },
];

/**
 * Maps the server's optional notes object onto display tiers (U-W11-T6).
 * Returns [] when the server publishes nothing usable: the PDP then renders
 * no pyramid section at all. The free-text prose field is never parsed into
 * tiers (§5.2 item 5; §12-G10: no structured notes ship today — gap logged
 * in BACKEND_REQUESTS.md).
 */
export function toOlfactoryTiers(notes: ProductNotes | undefined): PyramidTier[] {
  if (!notes) return [];
  const tiers: PyramidTier[] = [];
  for (const { key, index, label } of PYRAMID_TIERS) {
    const level = notes[key];
    if (!level || level.length === 0) continue;
    tiers.push({ key, index, label, notes: level });
  }
  return tiers;
}

/** SPEC-19-4 [R-19.11]: one armed preference on one product. */
export interface RestockPreference {
  product_id: number;
  slug: string;
  active: boolean;
}

export interface RestockOptIn {
  /**
   * True when the server signalled a registration: authed 201 (first
   * opt-in) or the guest 202 (uniform first-or-repeat, by design).
   * False only for the authed 200 re-arm of an existing preference.
   */
  created: boolean;
  preference: RestockPreference;
}

/**
 * Back-in-stock opt-in (§12.G6, SPEC-Q-RS1). Two live arms:
 * an AUTHENTICATED caller gets 201 on a first opt-in and 200 when an
 * existing preference is re-armed (their submitted email is ignored —
 * the trigger mails user.email); a GUEST caller who submits an email
 * gets one uniform 202, first-time or repeat, valid or invalid format —
 * deliberately zero oracle — and the trigger mails that address. A guest
 * POST with no submitted email (or an unparseable body) and every guest
 * DELETE stay 401. Throttled on two scopes: `restock` (per-IP anonymous,
 * per-user authed) plus `restock-guest` (per IP+email pair). The status
 * is captured via apiFetch's `onStatus` because the preference body is
 * identical at all three 2xx codes. Every 4xx arrives as the uniform
 * SPEC-9-03 envelope and surfaces as ApiError with the server's message
 * verbatim — the caller never rewrites it.
 */
export async function restockSubscribe(
  slug: string,
  email: string,
): Promise<RestockOptIn> {
  let status = 0;
  const preference = await apiFetch<RestockPreference>(
    `/api/products/${slug}/restock-notifications/`,
    {
      method: "POST",
      body: { email },
      auth: true,
      onStatus: (code) => {
        status = code;
      },
    },
  );
  return { created: status === 201 || status === 202, preference };
}

/**
 * Opt-out: a uniform, existence-hiding 200 (`{active: false}`) whether or
 * not a preference existed. Authenticated-only — a guest DELETE keeps the
 * 401 (opt-out is ownership-scoped to request.user; a guest row has no
 * caller identity to scope by).
 */
export function restockUnsubscribe(slug: string): Promise<{ active: boolean }> {
  return apiFetch<{ active: boolean }>(
    `/api/products/${slug}/restock-notifications/`,
    { method: "DELETE", auth: true },
  );
}
