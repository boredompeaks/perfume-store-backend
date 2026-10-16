/**
 * Flat brand + contact view of the white-label manifest in template.config.ts.
 * The export name and key shape are a public contract — every import of
 * `site` must keep compiling untouched — so derive, don't restructure here.
 * Env changes require a rebuild (next build).
 */
import { templateConfig } from "./template.config";

export const site = {
  name: templateConfig.brand.name,
  tagline: templateConfig.brand.tagline,
  description: templateConfig.brand.description,
  url: templateConfig.brand.url,

  /** Customer support inbox (also the SMTP sender address). */
  supportEmail: templateConfig.contact.supportEmail,

  /** Display phone number (e.g. "+91 98765 43210"). Empty = hidden in UI. */
  supportPhone: templateConfig.contact.supportPhone,

  /** WhatsApp number in international digits, no "+" (e.g. "919876543210"). Empty = hidden. */
  whatsappNumber: templateConfig.contact.whatsappNumber,

  /** Pre-filled message for the WhatsApp deep link. */
  whatsappMessage: templateConfig.contact.whatsappMessage,

  /** Full profile URL (e.g. "https://instagram.com/maisonaurel"). Empty = hidden. */
  instagramUrl: templateConfig.contact.instagramUrl,
} as const;
