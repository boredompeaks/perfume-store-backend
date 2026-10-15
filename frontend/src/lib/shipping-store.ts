import { site } from "./site";

/**
 * Checkout address prefill — handled as PII, per PLAN.md:
 * - a visible "saved on this device" disclosure at the point of prefill,
 * - an explicit "forget saved details" control,
 * - cleared on logout (see AuthProvider.logout).
 * Same-browser convenience only; never server truth.
 *
 * The key is derived from the configured brand so two white-label tenants
 * never read each other's saved address. The former hardcoded key
 * "aurel.shipping" is abandoned: entries written under it are left behind,
 * an acceptable prefill loss for white-label correctness.
 */

/**
 * Slugified storage key for a brand name — exported so tests can pin the
 * derivation (e.g. "Maison Aurel" → "maison.aurel.shipping").
 */
export function shippingStorageKey(brandName: string = site.name): string {
  const slug = brandName
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, ".")
    .replace(/^\.+|\.+$/g, "");
  return `${slug}.shipping`;
}

const KEY = shippingStorageKey();

export type ShippingDetails = {
  full_name: string;
  email: string;
  phone: string;
  address: string;
  city: string;
  state: string;
  pincode: string;
};

export function loadShipping(): ShippingDetails | null {
  try {
    const raw = localStorage.getItem(KEY);
    if (!raw) return null;
    const parsed: unknown = JSON.parse(raw);
    if (typeof parsed !== "object" || parsed === null) return null;
    const d = parsed as Partial<ShippingDetails>;
    if (typeof d.full_name !== "string" || d.full_name.trim() === "") {
      return null;
    }
    return {
      full_name: d.full_name,
      email: typeof d.email === "string" ? d.email : "",
      phone: typeof d.phone === "string" ? d.phone : "",
      address: typeof d.address === "string" ? d.address : "",
      city: typeof d.city === "string" ? d.city : "",
      state: typeof d.state === "string" ? d.state : "",
      pincode: typeof d.pincode === "string" ? d.pincode : "",
    };
  } catch {
    return null;
  }
}

export function saveShipping(details: ShippingDetails): void {
  try {
    localStorage.setItem(KEY, JSON.stringify(details));
  } catch {
    // Storage unavailable — prefill is a convenience, not a requirement.
  }
}

export function forgetShipping(): void {
  try {
    localStorage.removeItem(KEY);
  } catch {
    // ignore
  }
}

export function hasStoredShipping(): boolean {
  try {
    return localStorage.getItem(KEY) !== null;
  } catch {
    return false;
  }
}
