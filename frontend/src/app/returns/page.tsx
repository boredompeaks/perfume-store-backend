import type { Metadata } from "next";
import Link from "next/link";
import { getReturnWindow } from "@/lib/policies";
import { site } from "@/lib/site";

export const metadata: Metadata = {
  title: "Returns & Refunds",
};

/**
 * /returns — editorial policy spread (D1 design override of §5.7's
 * LegalShell composition; screens: maison_ther_politique_de_retour).
 * Same system as /shipping: bronze eyebrow, serif display h1, revision
 * line, a fact matrix at the top right, then anchor index + measure
 * prose with hairline seams. This stays a POLICY page, never a request
 * form (§A-14, §5.7): there is no customer-facing returns endpoint, so
 * the "how to request" path is the mailto below.
 *
 * The return window is the API's number or no number (A-12.D.2,
 * getReturnWindow): when `GET /api/settings/` publishes a positive
 * `return_window_days` it renders in the fact matrix AND the short
 * version; when it does not, both ship without a figure. No day count
 * is typed in this file. Motion: none (P2, §5.7). Server component.
 */

const sections = [
  {
    id: "the-short-version",
    num: "01",
    title: "The short version",
  },
  {
    id: "how-to-request-a-return",
    num: "02",
    title: "How to request a return",
  },
  {
    id: "refunds",
    num: "03",
    title: "Refunds",
  },
  {
    id: "damaged-or-wrong-item",
    num: "04",
    title: "Damaged or wrong item",
  },
] as const;

export default async function ReturnsPage() {
  // A-12.D.2: the window is stated only when the API publishes one; with no
  // number the same sentences ship minus their claim, never a fallback value.
  const { days } = await getReturnWindow();
  return (
    <div className="mx-auto w-full max-w-5xl px-4 py-16 sm:px-6">
      {/* Header band: left title spread, right fact matrix (screen header). */}
      <header className="flex flex-col gap-8 border-b border-hairline pb-10 lg:flex-row lg:items-end lg:justify-between">
        <div className="max-w-2xl">
          <p className="text-eyebrow uppercase text-bronze">
            {site.name} · Returns &amp; refunds
          </p>
          <h1 className="mt-3 font-display text-display tracking-tight">
            Returns &amp; Refunds
          </h1>
          <p className="mt-2 text-small text-ink-secondary">
            Last updated: September 2026
          </p>
        </div>
        <dl className="flex flex-col gap-6 sm:flex-row lg:flex-col">
          {/* The API's window, or the fact is omitted entirely. */}
          {days && (
            <div className="min-w-56">
              <dt className="text-eyebrow uppercase text-ink-secondary">
                Return window (unopened bottles)
              </dt>
              <dd className="mt-1 font-display text-h3 text-ink">
                {days} days from delivery
              </dd>
            </div>
          )}
          <div className="min-w-56">
            <dt className="text-eyebrow uppercase text-ink-secondary">
              Refunds
            </dt>
            <dd className="mt-1 font-display text-h3 text-ink">
              Original payment method
            </dd>
          </div>
        </dl>
      </header>

      <div className="grid gap-10 pt-10 lg:grid-cols-[200px_1fr] lg:gap-x-8">
        {/* Plain in-page anchor index, same pattern as /shipping. */}
        <nav
          aria-label="Returns sections"
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
          <section id="the-short-version" className="scroll-mt-24">
            <div className="flex items-baseline gap-2">
              <span className="text-eyebrow uppercase text-bronze">01</span>
              <h2 className="font-display text-h2 tracking-tight">
                The short version
              </h2>
            </div>
            <div className="mt-4 space-y-3 text-body leading-relaxed text-ink-secondary">
              <p>
                Unopened, unused perfume in its original packaging can be
                returned{days ? ` within ${days} days of delivery` : ""}.
                Refunds go back to your original payment method. Opened
                perfume boxes are non-returnable, for hygiene and
                authenticity reasons.
              </p>
            </div>
          </section>

          <hr className="border-t border-hairline" />
          <section id="how-to-request-a-return" className="scroll-mt-24 pt-10">
            <div className="flex items-baseline gap-2">
              <span className="text-eyebrow uppercase text-bronze">02</span>
              <h2 className="font-display text-h2 tracking-tight">
                How to request a return
              </h2>
            </div>
            <div className="mt-4 space-y-3 text-body leading-relaxed text-ink-secondary">
              <p>
                Email{" "}
                <a
                  href={`mailto:${site.supportEmail}?subject=Return request: Order #`}
                  className="text-ink underline underline-offset-4 transition-colors hover:text-bronze"
                >
                  {site.supportEmail}
                </a>{" "}
                with your order number and the reason. We confirm the return
                and the pickup/drop-off arrangement by email.
              </p>
            </div>
          </section>

          <hr className="border-t border-hairline" />
          <section id="refunds" className="scroll-mt-24 pt-10">
            <div className="flex items-baseline gap-2">
              <span className="text-eyebrow uppercase text-bronze">03</span>
              <h2 className="font-display text-h2 tracking-tight">Refunds</h2>
            </div>
            <div className="mt-4 space-y-3 text-body leading-relaxed text-ink-secondary">
              <p>
                Once the returned item reaches us and passes inspection, we
                issue the refund to your original payment method via our
                payment provider. Refunds are processed manually by our team
                and typically reflect in 5–7 business days depending on your
                bank.
              </p>
              <p>
                If a payment was captured but your order could not be
                fulfilled (an availability failure at confirmation), contact
                us with the order number shown in the error. These are
                reconciled with priority.
              </p>
            </div>
          </section>

          <hr className="border-t border-hairline" />
          <section id="damaged-or-wrong-item" className="scroll-mt-24 pt-10">
            <div className="flex items-baseline gap-2">
              <span className="text-eyebrow uppercase text-bronze">04</span>
              <h2 className="font-display text-h2 tracking-tight">
                Damaged or wrong item
              </h2>
            </div>
            <div className="mt-4 space-y-3 text-body leading-relaxed text-ink-secondary">
              <p>
                If your order arrives damaged, leaking, or isn&apos;t what you
                ordered, tell us within 48 hours of delivery with a photo. We
                replace it or refund it in full, your choice, including any
                return shipping cost.
              </p>
            </div>
          </section>
        </div>
      </div>

      {/* Support + cross-links, the screen's bottom band in English. */}
      <div className="mt-16 border-t border-hairline pt-6">
        <div className="flex flex-col gap-6 sm:flex-row sm:items-baseline sm:justify-between">
          <div>
            <p className="text-eyebrow uppercase text-ink-secondary">
              Returns concierge
            </p>
            <p className="mt-2 text-small text-ink-secondary">
              For any question about a return in progress, write to{" "}
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
              href="/shipping"
              className="text-ink underline underline-offset-4 transition-colors hover:text-bronze"
            >
              Shipping &amp; Delivery
            </Link>
          </div>
        </div>
      </div>
    </div>
  );
}
