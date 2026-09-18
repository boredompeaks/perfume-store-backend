import type { Metadata } from "next";
import { LegalSection, LegalShell } from "@/components/legal/LegalShell";

export const metadata: Metadata = {
  title: "Shipping",
};

export default function ShippingPage() {
  return (
    <LegalShell title="Shipping">
      <LegalSection title="Where we ship">
        <p>
          Across India, to the pincodes served by our courier partners. The
          delivery address and 6-digit pincode you enter at checkout are what
          we ship to — please double-check them.
        </p>
      </LegalSection>

      <LegalSection title="Dispatch and delivery">
        <p>
          Orders are typically dispatched within 2–4 business days of payment
          confirmation, and delivery usually takes a further 2–7 days
          depending on your location. Business days exclude Sundays and
          public holidays.
        </p>
      </LegalSection>

      <LegalSection title="Charges">
        <p>
          Shipping is currently included in the listed price — the total at
          checkout is the total you pay. If this ever changes, it will be
          shown clearly before payment.
        </p>
      </LegalSection>

      <LegalSection title="Tracking and problems">
        <p>
          For tracking or delivery questions, write to us with your order
          number. If a parcel arrives damaged or doesn&apos;t arrive, contact
          us within 48 hours of the expected delivery date and we will make
          it right — see the Returns page for the process.
        </p>
      </LegalSection>
    </LegalShell>
  );
}
