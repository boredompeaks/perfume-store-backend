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

/** One display tier of the olfactory pyramid. */
export type PyramidTier = {
  key: "top" | "heart" | "base";
  /** Wide-tracked indicator, e.g. "N° 01". */
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
 * Empty array means the PDP renders no pyramid section. The free-text prose
 * is never parsed into tiers; only structured notes count.
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

/** One armed preference on one product. */
export interface RestockPreference {
  product_id: number;
  slug: string;
  active: boolean;
}

export interface RestockOptIn {
  /**
   * True for the authed 201 and the guest 202; false only for the authed 200
   * re-arm of an existing preference.
   */
  created: boolean;
  preference: RestockPreference;
}

/**
 * Authed: 201 first opt-in, 200 re-arm. Guest: one uniform 202 whatever the
 * input (zero oracle). The body is identical across all three 2xx codes, so
 * `onStatus` captures which one answered. 4xx messages are the server's.
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
 * Uniform 200 whether or not a preference existed. Authenticated-only: a
 * guest DELETE stays 401 because opt-out is scoped to request.user.
 */
export function restockUnsubscribe(slug: string): Promise<{ active: boolean }> {
  return apiFetch<{ active: boolean }>(
    `/api/products/${slug}/restock-notifications/`,
    { method: "DELETE", auth: true },
  );
}
