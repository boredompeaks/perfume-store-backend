import Link from "next/link";
import ContactLinks from "./ContactLinks";
import { getContactSettings } from "@/lib/server-settings";
import { site } from "@/lib/site";

const shopLinks = [
  { href: "/products", label: "Shop all" },
  { href: "/products?ordering=-created_at", label: "New arrivals" },
  { href: "/cart", label: "Cart" },
  { href: "/orders", label: "Orders" },
  { href: "/login", label: "Sign in" },
];

const helpLinks = [
  { href: "/contact", label: "Contact" },
  { href: "/shipping", label: "Shipping" },
  { href: "/returns", label: "Returns & Refunds" },
];

const legalLinks = [
  { href: "/privacy", label: "Privacy Policy" },
  { href: "/terms", label: "Terms & Conditions" },
];

export default async function Footer() {
  const contact = await getContactSettings();

  return (
    <footer className="border-t border-line">
      <div className="mx-auto grid max-w-6xl gap-10 px-4 py-14 sm:px-6 md:grid-cols-[2fr_1fr_1fr_1fr]">
        <div>
          <p className="font-display text-lg tracking-tight">
            {site.name}<span aria-hidden="true">™</span>
          </p>
          <p className="mt-2 max-w-xs text-sm leading-relaxed text-ink-muted">
            {site.tagline}. Fewer materials, longer macerations, no seasonal
            noise.
          </p>
          <p className="mt-6 text-xs uppercase tracking-[0.15em] text-ink-muted">
            Get in touch
          </p>
          <ContactLinks className="mt-3" settings={contact} />
        </div>

        <nav aria-label="Shop" className="flex flex-col items-start gap-2 text-sm">
          <p className="text-xs uppercase tracking-[0.15em] text-ink-muted">Shop</p>
          {shopLinks.map((link) => (
            <Link
              key={link.label}
              href={link.href}
              className="link-underline transition-colors hover:text-bronze"
            >
              {link.label}
            </Link>
          ))}
        </nav>

        <nav aria-label="Help" className="flex flex-col items-start gap-2 text-sm">
          <p className="text-xs uppercase tracking-[0.15em] text-ink-muted">Help</p>
          {helpLinks.map((link) => (
            <Link
              key={link.label}
              href={link.href}
              className="link-underline transition-colors hover:text-bronze"
            >
              {link.label}
            </Link>
          ))}
          <a
            href={`mailto:${site.supportEmail}`}
            className="link-underline transition-colors hover:text-bronze"
          >
            Email support
          </a>
        </nav>

        <nav aria-label="Legal" className="flex flex-col items-start gap-2 text-sm">
          <p className="text-xs uppercase tracking-[0.15em] text-ink-muted">Legal</p>
          {legalLinks.map((link) => (
            <Link
              key={link.label}
              href={link.href}
              className="link-underline transition-colors hover:text-bronze"
            >
              {link.label}
            </Link>
          ))}
          <p className="text-xs leading-relaxed text-ink-muted">
            Payments secured by Razorpay
          </p>
        </nav>
      </div>

      <div className="border-t border-line">
        <div className="mx-auto flex max-w-6xl flex-wrap items-center justify-between gap-2 px-4 py-5 text-xs text-ink-muted sm:px-6">
          <p>
            © {new Date().getFullYear()} {site.name}
            <span aria-hidden="true">™</span>. All rights reserved.
          </p>
          <p>
            {site.name} and the {site.name} wordmark are trademarks of{" "}
            {site.name}. Prices in INR · Payments via Razorpay
          </p>
        </div>
      </div>
    </footer>
  );
}
