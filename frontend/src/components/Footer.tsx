import Link from "next/link";
import ContactLinks from "./ContactLinks";
import { getContactSettings } from "@/lib/server-settings";
import { site } from "@/lib/site";
import { templateConfig } from "@/lib/template.config";

export default async function Footer() {
  const contact = await getContactSettings();

  return (
    <footer className="border-t border-hairline">
      <div className="mx-auto grid max-w-6xl gap-10 px-4 py-14 sm:px-6 md:grid-cols-[2fr_1fr_1fr_1fr]">
        <div>
          <p className="font-display text-lg tracking-tight">
            {site.name}<span aria-hidden="true">™</span>
          </p>
          <p className="mt-2 max-w-xs text-sm leading-relaxed text-ink-secondary">
            {site.tagline}. Fewer materials, longer macerations, no seasonal
            noise.
          </p>
          <p className="mt-6 text-xs uppercase tracking-[0.15em] text-ink-secondary">
            Get in touch
          </p>
          <ContactLinks className="mt-3" settings={contact} />
        </div>

        <nav aria-label="Shop" className="flex flex-col items-start gap-2 text-sm">
          <p className="text-xs uppercase tracking-[0.15em] text-ink-secondary">Shop</p>
          {templateConfig.footer.shop.map((link) => (
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
          <p className="text-xs uppercase tracking-[0.15em] text-ink-secondary">Help</p>
          {templateConfig.footer.help.map((link) => (
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
          <p className="text-xs uppercase tracking-[0.15em] text-ink-secondary">Legal</p>
          {templateConfig.footer.legal.map((link) => (
            <Link
              key={link.label}
              href={link.href}
              className="link-underline transition-colors hover:text-bronze"
            >
              {link.label}
            </Link>
          ))}
          <p className="text-xs leading-relaxed text-ink-secondary">
            Payments secured by Razorpay
          </p>
        </nav>
      </div>

      <div className="border-t border-hairline">
        <div className="mx-auto flex max-w-6xl flex-wrap items-center justify-between gap-2 px-4 py-5 text-xs text-ink-secondary sm:px-6">
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
