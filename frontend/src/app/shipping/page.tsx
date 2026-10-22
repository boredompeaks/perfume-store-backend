import type { Metadata } from "next";
import Link from "next/link";
import { shippingPolicy } from "@/lib/policies";
import { site } from "@/lib/site";

export const metadata: Metadata = {
  title: "Shipping",
};

/**
 * /shipping — editorial policy spread (D1 design override of §5.7's
 * LegalShell composition; screens: maison_ther_politique_de_livraison).
 * The typography system the screens establish: bronze eyebrow, serif
 * display h1, revision line, then a right-hand FACT MATRIX carrying the
 * two scannable windows at the top, because those are the only things a
 * customer opens this page to find. Below the hairline seam: a plain
 * anchor index (the W8-T3 no-JS ToC pattern, distinct nav name) beside
 * measure-limited prose, sections ruled apart by hairlines.
 *
 * The windows render from `shippingPolicy` (policies.ts, the A-12.D.2
 * single source) in BOTH the fact matrix and the dispatch sentence —
 * neither number is typed here (policies.test.ts pins the source).
 * Content stays the shipped truth: India courier network, charges
 * included, no transcribed screen windows (the screen's French
 * per-region delays have no backend source and are not restated).
 *
 * Motion: none. Zero accent moments (P2's wrong-when clause, §5.7): a
 * legal page that animates is a legal page nobody trusts. The bottom
 * block keeps LegalShell's support + cross-link duties in the screen's
 * two-column form. Server component; no client island.
 */

const sections = [
  {
    id: "where-we-ship",
    num: "01",
    title: "Where we ship",
    body: (
      <p>
        Across India, to the pincodes served by our courier partners. The
        delivery address and 6-digit pincode you enter at checkout are what
        we ship to. Please double-check them.
      </p>
    ),
  },
  {
    id: "dispatch-and-delivery",
    num: "02",
    title: "Dispatch and delivery",
    body: (
      <p>
        Orders are typically dispatched within {shippingPolicy.dispatchWindow}{" "}
        of payment confirmation, and delivery usually takes a further{" "}
        {shippingPolicy.deliveryWindow} depending on your location. Business
        days exclude Sundays and public holidays.
      </p>
    ),
  },
  {
    id: "charges",
    num: "03",
    title: "Charges",
    body: (
      <p>
        Shipping is currently included in the listed price: the total at
        checkout is the total you pay. If this ever changes, it will be shown
        clearly before payment.
      </p>
    ),
  },
  {
    id: "tracking-and-problems",
    num: "04",
    title: "Tracking and problems",
    body: (
      <p>
        For tracking or delivery questions, write to us with your order
        number, or look the parcel up on the{" "}
        <Link
          href="/track-order"
          className="text-ink underline underline-offset-4 transition-colors hover:text-bronze"
        >
          tracking page
        </Link>
        . If a parcel arrives damaged or doesn&apos;t arrive, contact us
        within 48 hours of the expected delivery date and we will make it
        right. See the{" "}
        <Link
          href="/returns"
          className="text-ink underline underline-offset-4 transition-colors hover:text-bronze"
        >
          Returns page
        </Link>{" "}
        for the process.
      </p>
    ),
  },
] as const;

export default function ShippingPage() {
  return (
    <div className="mx-auto w-full max-w-5xl px-4 py-16 sm:px-6">
      {/* Header band: left title spread, right fact matrix (screen header). */}
      <header className="flex flex-col gap-8 border-b border-hairline pb-10 lg:flex-row lg:items-end lg:justify-between">
        <div className="max-w-2xl">
          <p className="text-eyebrow uppercase text-bronze">
            {site.name} · Shipping &amp; consignment
          </p>
          <h1 className="mt-3 font-display text-display tracking-tight">
            Shipping &amp; Delivery
          </h1>
          <p className="mt-2 text-small text-ink-secondary">
            Last updated: September 2026
          </p>
        </div>
        <dl className="flex flex-col gap-6 sm:flex-row lg:flex-col">
          <div className="min-w-56">
            <dt className="text-eyebrow uppercase text-ink-secondary">
              Dispatch window
            </dt>
            <dd className="mt-1 font-display text-h3 text-ink">
              {shippingPolicy.dispatchWindow}
            </dd>
          </div>
          <div className="min-w-56">
            <dt className="text-eyebrow uppercase text-ink-secondary">
              Delivery window
            </dt>
            <dd className="mt-1 font-display text-h3 text-ink">
              {shippingPolicy.deliveryWindow}
            </dd>
          </div>
        </dl>
      </header>

      <div className="grid gap-10 pt-10 lg:grid-cols-[200px_1fr] lg:gap-x-8">
        {/* Plain in-page anchor index: no scroll spy, no observer (the
            W8-T3 LegalProse pattern; hidden on phones where a list this
            short is worse than none). */}
        <nav
          aria-label="Shipping sections"
          className="hidden text-small text-ink-secondary md:block lg:sticky lg:top-24 lg:self-start"
        >
          <p className="text-eyebrow uppercase text-ink-secondary">
            On this page
          </p>
          <ol className="mt-4 space-y-1">
            {sections.map((section) => (
              <li key={section.id}>
                <a
                  href={`#${section.id}`}
                  className="block py-2.5 transition-colors hover:text-bronze"
                >
                  {section.num}. {section.title}
                </a>
              </li>
            ))}
          </ol>
        </nav>

        {/* Measure-limited prose, hairline-ruled sections (screen). */}
        <div className="min-w-0 max-w-(--measure) space-y-10">
          {sections.map((section, i) => (
            <div key={section.id}>
              {i > 0 && <hr className="border-t border-hairline" />}
              <section
                id={section.id}
                className={i > 0 ? "scroll-mt-24 pt-10" : "scroll-mt-24"}
              >
                <div className="flex items-baseline gap-2">
                  <span className="text-eyebrow uppercase text-bronze">
                    {section.num}
                  </span>
                  <h2 className="font-display text-h2 tracking-tight">
                    {section.title}
                  </h2>
                </div>
                <div className="mt-4 space-y-3 text-body leading-relaxed text-ink-secondary">
                  {section.body}
                </div>
              </section>
            </div>
          ))}
        </div>
      </div>

      {/* Support + cross-links, the screen's bottom band in English. */}
      <div className="mt-16 border-t border-hairline pt-6">
        <div className="flex flex-col gap-6 sm:flex-row sm:items-baseline sm:justify-between">
          <div>
            <p className="text-eyebrow uppercase text-ink-secondary">
              Shipping concierge
            </p>
            <p className="mt-2 text-small text-ink-secondary">
              For any question about a parcel in transit, write to{" "}
              <a
                href={`mailto:${site.supportEmail}`}
                className="text-ink underline underline-offset-4 transition-colors hover:text-bronze"
              >
                {site.supportEmail}
              </a>
              .
            </p>
          </div>
          <div className="flex flex-wrap gap-x-8 gap-y-2 text-small">
            <Link
              href="/terms"
              className="text-ink underline underline-offset-4 transition-colors hover:text-bronze"
            >
              Terms &amp; Conditions
            </Link>
            <Link
              href="/privacy"
              className="text-ink underline underline-offset-4 transition-colors hover:text-bronze"
            >
              Privacy Policy
            </Link>
            <Link
              href="/returns"
              className="text-ink underline underline-offset-4 transition-colors hover:text-bronze"
            >
              Returns &amp; Refunds
            </Link>
          </div>
        </div>
      </div>
    </div>
  );
}
