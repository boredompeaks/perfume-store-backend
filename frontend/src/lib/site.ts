export const site = {
  name: "Maison Aurel",
  tagline: "Perfumes composed with restraint",
  description:
    "Maison Aurel is a small perfume house. Explore eau de parfum, browse new arrivals, and order with secure checkout.",
  url: process.env.NEXT_PUBLIC_SITE_URL ?? "http://localhost:3000",
} as const;
