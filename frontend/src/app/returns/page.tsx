import type { Metadata } from "next";
import { LegalSection, LegalShell } from "@/components/legal/LegalShell";
import { site } from "@/lib/site";

export const metadata: Metadata = {
  title: "Returns & Refunds",
};

export default function ReturnsPage() {
  return (
    <LegalShell title="Returns & Refunds">
      <LegalSection title="The short version">
        <p>
          Unopened, unused perfume in its original packaging can be returned
          within 7 days of delivery. Refunds go back to your original payment
          method. Opened perfume boxes are non-returnable, for hygiene and
          authenticity reasons.
        </p>
      </LegalSection>

      <LegalSection title="How to request a return">
        <p>
          Email{" "}
          <a
            href={`mailto:${site.supportEmail}?subject=Return request — Order #`}
            className="text-ink underline underline-offset-4 transition-colors hover:text-bronze"
          >
            {site.supportEmail}
          </a>{" "}
          with your order number and the reason. We confirm the return and
          the pickup/drop-off arrangement by email.
        </p>
      </LegalSection>

      <LegalSection title="Refunds">
        <p>
          Once the returned item reaches us and passes inspection, we issue
          the refund to your original payment method via our payment
          provider. Refunds are processed manually by our team and typically
          reflect in 5–7 business days depending on your bank.
        </p>
        <p>
          If a payment was captured but your order could not be fulfilled
          (an availability failure at confirmation), contact us with the
          order number shown in the error — these are reconciled with
          priority.
        </p>
      </LegalSection>

      <LegalSection title="Damaged or wrong item">
        <p>
          If your order arrives damaged, leaking, or isn&apos;t what you
          ordered, tell us within 48 hours of delivery with a photo. We
          replace it or refund it in full — your choice — including any
          return shipping cost.
        </p>
      </LegalSection>
    </LegalShell>
  );
}
