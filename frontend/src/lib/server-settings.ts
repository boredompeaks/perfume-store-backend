import { API_BASE } from "./config";
import { site } from "./site";

export type ContactSettings = {
  supportEmail: string;
  supportPhone: string;
  whatsappNumber: string;
  whatsappMessage: string;
  instagramUrl: string;
};

export const fallbackContact: ContactSettings = {
  supportEmail: site.supportEmail,
  supportPhone: site.supportPhone,
  whatsappNumber: site.whatsappNumber,
  whatsappMessage: site.whatsappMessage,
  instagramUrl: site.instagramUrl,
};

/**
 * Contact channels, in priority order:
 *   Django admin (Site settings) → build-time env (site.ts) → hidden.
 * Fetched server-side with a 60s cache; if the backend is unreachable the
 * env defaults keep the footer rendering.
 */
export async function getContactSettings(): Promise<ContactSettings> {
  try {
    const res = await fetch(`${API_BASE}/api/settings/`, {
      next: { revalidate: 60 },
    });
    if (!res.ok) return fallbackContact;
    const data = await res.json();
    const pick = (apiValue: unknown, fallback: string): string =>
      typeof apiValue === "string" && apiValue.trim() !== "" ? apiValue : fallback;
    return {
      supportEmail: pick(data.support_email, site.supportEmail),
      supportPhone: pick(data.support_phone, site.supportPhone),
      whatsappNumber: pick(data.whatsapp_number, site.whatsappNumber),
      whatsappMessage: pick(data.whatsapp_message, site.whatsappMessage),
      instagramUrl: pick(data.instagram_url, site.instagramUrl),
    };
  } catch {
    return fallbackContact;
  }
}
