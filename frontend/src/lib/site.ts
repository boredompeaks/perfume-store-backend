/**
 * Single source of truth for brand + contact details. Everything here can be
 * overridden repo-wide through NEXT_PUBLIC_* env vars (.env.local) — no other
 * file hardcodes contact info. Env changes require a rebuild (next build).
 */
function env(name: string): string {
  const value = process.env[name];
  return typeof value === "string" && value.trim() !== "" ? value.trim() : "";
}

export const site = {
  name: "Maison Aurel",
  tagline: "Perfumes composed with restraint",
  description:
    "Maison Aurel is a small perfume house. Explore eau de parfum, browse new arrivals, and order with secure checkout.",
  url: env("NEXT_PUBLIC_SITE_URL") || "http://localhost:3000",

  /** Customer support inbox (also the SMTP sender address). */
  supportEmail: env("NEXT_PUBLIC_SUPPORT_EMAIL") || "biochem1981@gmail.com",

  /** Display phone number (e.g. "+91 98765 43210"). Empty = hidden in UI. */
  supportPhone: env("NEXT_PUBLIC_SUPPORT_PHONE"),

  /** WhatsApp number in international digits, no "+" (e.g. "919876543210"). Empty = hidden. */
  whatsappNumber: env("NEXT_PUBLIC_WHATSAPP_NUMBER"),

  /** Pre-filled message for the WhatsApp deep link. */
  whatsappMessage:
    env("NEXT_PUBLIC_WHATSAPP_MESSAGE") ||
    "Hi! I have a question about a fragrance.",

  /** Full profile URL (e.g. "https://instagram.com/maisonaurel"). Empty = hidden. */
  instagramUrl: env("NEXT_PUBLIC_INSTAGRAM_URL"),
} as const;
