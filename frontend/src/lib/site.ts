/**
 * Flat view of the white-label manifest; the export name and key shape are a
 * public contract, so derive rather than restructure. Env is baked at build.
 */
import { templateConfig } from "./template.config";

export const site = {
  name: templateConfig.brand.name,
  tagline: templateConfig.brand.tagline,
  description: templateConfig.brand.description,
  url: templateConfig.brand.url,

  /** Customer support inbox (also the SMTP sender address). */
  supportEmail: templateConfig.contact.supportEmail,

  /** Empty hides the channel in the UI. */
  supportPhone: templateConfig.contact.supportPhone,

  /** International digits, no "+"; empty hides. */
  whatsappNumber: templateConfig.contact.whatsappNumber,

  whatsappMessage: templateConfig.contact.whatsappMessage,

  /** Empty hides the channel in the UI. */
  instagramUrl: templateConfig.contact.instagramUrl,
} as const;
