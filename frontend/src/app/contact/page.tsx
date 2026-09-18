import type { Metadata } from "next";
import ContactLinks from "@/components/ContactLinks";
import { LegalSection, LegalShell } from "@/components/legal/LegalShell";
import { getContactSettings } from "@/lib/server-settings";
import { site } from "@/lib/site";

export const metadata: Metadata = {
  title: "Contact",
};

export default async function ContactPage() {
  const contact = await getContactSettings();

  return (
    <LegalShell title="Contact">
      <LegalSection title="Reach us directly">
        <p>WhatsApp, call, email, or Instagram — whatever suits you.</p>
        <ContactLinks className="mt-4" settings={contact} />
        {contact.supportPhone && (
          <p className="mt-3 text-sm">
            Phone:{" "}
            <span className="text-ink">{contact.supportPhone}</span> (10am–6pm,
            Mon–Sat)
          </p>
        )}
      </LegalSection>

      <LegalSection title="Customer support">
        <p>
          For orders, returns, tracking, or anything else, write to{" "}
          <a
            href={`mailto:${site.supportEmail}`}
            className="text-ink underline underline-offset-4 transition-colors hover:text-bronze"
          >
            {site.supportEmail}
          </a>
          . We reply within 1–2 business days.
        </p>
        <p>
          Order questions are fastest to resolve when you include the order
          number (visible on the order page and in your order history).
        </p>
      </LegalSection>

      <LegalSection title="Payments">
        <p>
          Payment failures, double charges, or refund status — email us with
          the order number and (if you have it) the Razorpay payment
          reference. We reconcile every payment manually against our records.
        </p>
      </LegalSection>

      <LegalSection title="The house">
        <p>
          {site.name} is a small perfume house selling eau de parfum across
          India. Everything on this site is dispatched from our workshop —
          if you want to know what&apos;s in a bottle before buying, ask.
        </p>
      </LegalSection>
    </LegalShell>
  );
}
